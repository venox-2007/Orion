"""Native Windows Screen OCR engine and text localization for ORION (Step 32).

Uses Windows 11's built-in WinRT Windows.Media.Ocr engine with zero external
dependencies (no Tesseract or manual binary installations required).
"""

from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
import json
import logging
from pathlib import Path
import subprocess
import tempfile
import uuid
from typing import Any

from PIL import ImageGrab
from orion.tools import input_control

logger = logging.getLogger("orion.tools.screen_ocr")

WINRT_OCR_SCRIPT_PATH = Path(__file__).parent / "winrt_ocr.ps1"


class RECT(ctypes.Structure):
    """Windows RECT structure for window coordinate bounding boxes."""

    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


def _enable_dpi_awareness() -> None:
    """Ensure process is DPI-aware so GetWindowRect returns unscaled physical screen coordinates."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def capture_target_region(target_app: str | None = None) -> tuple[Path, tuple[int, int]]:
    """Capture a screenshot of a target application window or the full screen.

    Returns:
        (temp_png_path, (offset_x, offset_y))
    """
    _enable_dpi_awareness()
    temp_dir = Path(tempfile.gettempdir())
    temp_path = temp_dir / f"orion_ocr_{uuid.uuid4().hex[:8]}.png"

    offset_x, offset_y = 0, 0

    if target_app:
        try:
            input_control.focus_window_by_name(target_app)
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if hwnd:
                rect = RECT()
                if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
                    width = right - left
                    height = bottom - top
                    if width > 10 and height > 10:
                        img = ImageGrab.grab(bbox=(left, top, right, bottom))
                        img.save(temp_path, format="PNG")
                        return temp_path, (left, top)
        except Exception as e:
            logger.debug("Failed to capture specific window for %s: %s", target_app, e)

    # Fallback: capture primary display / all screens
    try:
        img = ImageGrab.grab(all_screens=True)
    except Exception:
        img = ImageGrab.grab()

    img.save(temp_path, format="PNG")
    return temp_path, (offset_x, offset_y)


def _run_winrt_ocr_ps(image_path: Path) -> dict[str, Any]:
    """Execute PowerShell WinRT OCR script synchronously and parse JSON output."""
    if not WINRT_OCR_SCRIPT_PATH.exists():
        raise FileNotFoundError(f"WinRT OCR PowerShell script not found: {WINRT_OCR_SCRIPT_PATH}")

    cmd = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(WINRT_OCR_SCRIPT_PATH),
        "-ImagePath",
        str(image_path),
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        stdout = (proc.stdout or "").strip()
        if not stdout:
            err_msg = (proc.stderr or "").strip() or "Empty output from OCR script."
            return {"success": False, "error": err_msg, "text": "", "lines": [], "words": []}

        # Locate valid JSON payload in output
        json_start = stdout.find("{")
        if json_start != -1:
            json_str = stdout[json_start:]
            data = json.loads(json_str)
            return data

        return {"success": False, "error": f"Invalid JSON output: {stdout}", "text": "", "lines": [], "words": []}
    except Exception as e:
        logger.warning("WinRT OCR execution failed: %s", e)
        return {"success": False, "error": str(e), "text": "", "lines": [], "words": []}


def extract_screen_text_sync(target_app: str | None = None) -> dict[str, Any]:
    """Capture screen/window, execute WinRT OCR, and adjust word coordinates into absolute screen space."""
    temp_img_path, (offset_x, offset_y) = capture_target_region(target_app=target_app)
    try:
        raw_result = _run_winrt_ocr_ps(temp_img_path)
        if not raw_result.get("success"):
            return {
                "success": False,
                "text": "",
                "lines": [],
                "words": [],
                "error": raw_result.get("error", "OCR failed"),
            }

        lines = raw_result.get("lines", [])
        words = raw_result.get("words", [])

        # Offset words to absolute screen coordinates
        for w in words:
            w["x"] = int(w.get("x", 0)) + offset_x
            w["y"] = int(w.get("y", 0)) + offset_y
            w["w"] = int(w.get("w", 0))
            w["h"] = int(w.get("h", 0))
            w["center_x"] = w["x"] + w["w"] // 2
            w["center_y"] = w["y"] + w["h"] // 2

        for line in lines:
            for lw in line.get("words", []):
                lw["x"] = int(lw.get("x", 0)) + offset_x
                lw["y"] = int(lw.get("y", 0)) + offset_y
                lw["w"] = int(lw.get("w", 0))
                lw["h"] = int(lw.get("h", 0))
                lw["center_x"] = lw["x"] + lw["w"] // 2
                lw["center_y"] = lw["y"] + lw["h"] // 2

        return {
            "success": True,
            "text": raw_result.get("text", "").strip(),
            "lines": lines,
            "words": words,
        }
    finally:
        try:
            if temp_img_path.exists():
                temp_img_path.unlink()
        except Exception:
            pass


async def extract_screen_text(target_app: str | None = None) -> dict[str, Any]:
    """Asynchronously extract text and grounded bounding boxes from target window or screen."""
    return await asyncio.to_thread(extract_screen_text_sync, target_app)


def find_text_coordinates_sync(query: str, target_app: str | None = None) -> tuple[int, int] | None:
    """Locate screen center coordinates of query text via OCR (exact or multi-word phrase match)."""
    clean_query = query.strip().lower().strip("'\"")
    if not clean_query:
        return None

    ocr_res = extract_screen_text_sync(target_app=target_app)
    if not ocr_res.get("success"):
        return None

    words = ocr_res.get("words", [])
    lines = ocr_res.get("lines", [])

    # 1. Exact single word match
    for w in words:
        if w.get("text", "").strip().lower() == clean_query:
            return w["center_x"], w["center_y"]

    # 2. Multi-word phrase matching across consecutive words in lines
    query_tokens = clean_query.split()
    n_tokens = len(query_tokens)

    if n_tokens > 1:
        for line in lines:
            l_words = line.get("words", [])
            if len(l_words) < n_tokens:
                continue

            for i in range(len(l_words) - n_tokens + 1):
                window_words = l_words[i : i + n_tokens]
                window_text = " ".join(w.get("text", "").strip().lower() for w in window_words)
                if window_text == clean_query or clean_query in window_text:
                    min_x = min(w["x"] for w in window_words)
                    min_y = min(w["y"] for w in window_words)
                    max_x = max(w["x"] + w["w"] for w in window_words)
                    max_y = max(w["y"] + w["h"] for w in window_words)
                    return (min_x + max_x) // 2, (min_y + max_y) // 2

        # Check line text substring match
        for line in lines:
            if clean_query in line.get("text", "").strip().lower():
                l_words = line.get("words", [])
                if l_words:
                    min_x = min(w["x"] for w in l_words)
                    min_y = min(w["y"] for w in l_words)
                    max_x = max(w["x"] + w["w"] for w in l_words)
                    max_y = max(w["y"] + w["h"] for w in l_words)
                    return (min_x + max_x) // 2, (min_y + max_y) // 2

    # 3. Substring match within single words
    for w in words:
        w_text = w.get("text", "").strip().lower()
        if clean_query in w_text or (len(w_text) >= 4 and w_text in clean_query):
            return w["center_x"], w["center_y"]

    return None


async def find_text_coordinates(query: str, target_app: str | None = None) -> tuple[int, int] | None:
    """Asynchronously find center coordinates of query text via OCR."""
    return await asyncio.to_thread(find_text_coordinates_sync, query, target_app)
