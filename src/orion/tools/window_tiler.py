"""Spatial window tiling and workspace layout management for ORION (Step 35)."""

from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

from orion.tools import desktop_inspector, input_control

logger = logging.getLogger("orion.tools.window_tiler")

# Windows API constants
SPI_GETWORKAREA = 0x0030
HWND_TOP = 0
SWP_SHOWWINDOW = 0x0040
SW_RESTORE = 9
SW_MAXIMIZE = 3
SW_MINIMIZE = 6


class RECT(ctypes.Structure):
    """Windows RECT structure for screen and window coordinate rectangles."""

    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


def _enable_dpi_awareness() -> None:
    """Ensure process is DPI-aware for physical desktop pixel coordinates."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def get_work_area() -> tuple[int, int, int, int]:
    """Query usable desktop work area (left, top, right, bottom) excluding the taskbar."""
    if sys.platform != "win32":
        return (0, 0, 1920, 1080)

    _enable_dpi_awareness()
    rect = RECT()
    user32 = ctypes.windll.user32
    try:
        success = user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
        if success:
            return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
    except Exception as err:
        logger.debug("SystemParametersInfoW failed: %s", err)

    # Fallback to display metrics
    try:
        w = user32.GetSystemMetrics(0)
        h = user32.GetSystemMetrics(1)
        return (0, 0, int(w), int(h))
    except Exception:
        return (0, 0, 1920, 1080)


def calculate_tile_geometry(
    position: str,
    work_area: tuple[int, int, int, int] | None = None,
) -> tuple[int, int, int, int] | None:
    """Calculate (x, y, width, height) coordinates for a given screen position."""
    if work_area is None:
        work_area = get_work_area()

    left, top, right, bottom = work_area
    width = right - left
    height = bottom - top

    pos = position.strip().lower().replace("_", "-")

    if pos in ("left", "left-half"):
        return (left, top, width // 2, height)
    elif pos in ("right", "right-half"):
        return (left + width // 2, top, width - width // 2, height)
    elif pos in ("top", "top-half"):
        return (left, top, width, height // 2)
    elif pos in ("bottom", "bottom-half"):
        return (left, top + height // 2, width, height - height // 2)
    elif pos in ("top-left", "topleft"):
        return (left, top, width // 2, height // 2)
    elif pos in ("top-right", "topright"):
        return (left + width // 2, top, width - width // 2, height // 2)
    elif pos in ("bottom-left", "bottomleft"):
        return (left, top + height // 2, width // 2, height - height // 2)
    elif pos in ("bottom-right", "bottomright"):
        return (left + width // 2, top + height // 2, width - width // 2, height - height // 2)
    elif pos == "center":
        w = int(width * 0.70)
        h = int(height * 0.80)
        x = left + (width - w) // 2
        y = top + (height - h) // 2
        return (x, y, w, h)
    elif pos in ("maximize", "full", "maximized"):
        return None

    raise ValueError(f"Unknown tile position: '{position}'")


def find_target_window(app_name: str) -> tuple[int | None, str]:
    """Find active window handle and display title matching app_name."""
    clean = app_name.strip()
    clean_lower = clean.lower()

    user32 = getattr(ctypes.windll, "user32", None)
    if not user32:
        return None, clean

    # 1. Target is active window
    if clean_lower in ("active", "current", "this", "active window", "foreground"):
        hwnd = user32.GetForegroundWindow()
        if hwnd:
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buff = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buff, length + 1)
                return hwnd, buff.value
            return hwnd, "Active Window"
        return None, "Active Window"

    name_mappings = {
        "notepad": ["notepad"],
        "chrome": ["chrome", "google chrome"],
        "brave": ["brave", "brave browser"],
        "code": ["visual studio code", "vscode", "code"],
        "vscode": ["visual studio code", "vscode", "code"],
        "telegram": ["telegram"],
        "word": ["word", "winword"],
        "excel": ["excel"],
        "powerpoint": ["powerpoint"],
        "calc": ["calculator"],
        "calculator": ["calculator"],
        "edge": ["edge", "microsoft edge"],
        "terminal": ["terminal", "powershell", "cmd"],
        "spotify": ["spotify"],
    }
    match_keywords = name_mappings.get(clean_lower, [clean_lower])

    # 2. Search approved apps via desktop_inspector
    try:
        wins = desktop_inspector._enumerate_windows_native()
        for w in wins:
            w_title_lower = w.title.lower()
            w_app_lower = w.app_name.lower()
            w_id_lower = w.app_id.lower()
            for kw in match_keywords:
                if kw in w_title_lower or kw in w_app_lower or kw == w_id_lower:
                    return w.hwnd, w.title or w.app_name
    except Exception as e:
        logger.debug("Desktop inspector enumeration error: %s", e)

    # 3. Fallback: EnumWindows by window title
    found_hwnd = None
    found_title = clean

    def enum_cb(hwnd: int, lparam: int) -> bool:
        nonlocal found_hwnd, found_title
        if hasattr(user32, "IsWindowVisible") and not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buff = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buff, length + 1)
            t_lower = buff.value.lower()
            for kw in match_keywords:
                if kw in t_lower:
                    found_hwnd = hwnd
                    found_title = buff.value
                    return False
        return True

    try:
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        cb_func = WNDENUMPROC(enum_cb)
        user32.EnumWindows(cb_func, 0)
    except Exception as e:
        logger.debug("EnumWindows fallback error: %s", e)

    return found_hwnd, found_title


def tile_window(app_name: str, position: str) -> tuple[bool, str]:
    """Snap/tile a target window to a specified desktop screen position."""
    hwnd, disp_title = find_target_window(app_name)
    if not hwnd:
        return False, f"⚠️ Could not find an active window for *'{app_name}'*."

    user32 = ctypes.windll.user32
    norm_pos = position.strip().lower()

    try:
        # Restore window if minimized or maximized
        if hasattr(user32, "IsIconic") and user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        elif hasattr(user32, "IsZoomed") and user32.IsZoomed(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)

        if norm_pos in ("maximize", "full", "maximized"):
            user32.ShowWindow(hwnd, SW_MAXIMIZE)
            input_control.force_foreground_window(hwnd)
            return True, f"🪟 Maximized *{disp_title}* to full screen."

        coords = calculate_tile_geometry(norm_pos)
        if coords is None:
            user32.ShowWindow(hwnd, SW_MAXIMIZE)
            input_control.force_foreground_window(hwnd)
            return True, f"🪟 Maximized *{disp_title}* to full screen."

        x, y, w, h = coords
        user32.SetWindowPos(hwnd, HWND_TOP, x, y, w, h, SWP_SHOWWINDOW)
        input_control.force_foreground_window(hwnd)
        return True, f"🪟 Snapped *{disp_title}* to the *{norm_pos}*."
    except Exception as err:
        logger.error("Failed to tile window %s (%s): %s", hwnd, app_name, err)
        return False, f"⚠️ Failed to tile window *'{app_name}'*: {err}"


def _launch_brave_if_needed() -> None:
    """Ensure Brave browser is launched if no window currently exists."""
    hwnd_brave, _ = find_target_window("brave")
    if hwnd_brave:
        return
    from orion.tools import app_launcher

    res = app_launcher.launch_application("brave")
    if res.get("success"):
        return

    # Direct launch fallback for standard Windows Brave installation paths
    cand_paths = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "BraveSoftware"
        / "Brave-Browser"
        / "Application"
        / "brave.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "BraveSoftware"
        / "Brave-Browser"
        / "Application"
        / "brave.exe",
    ]
    local_ad = os.environ.get("LOCALAPPDATA")
    if local_ad:
        cand_paths.append(
            Path(local_ad)
            / "BraveSoftware"
            / "Brave-Browser"
            / "Application"
            / "brave.exe"
        )
    for cp in cand_paths:
        if cp.is_file():
            try:
                subprocess.Popen([str(cp)])
                break
            except Exception as e:
                logger.debug("Failed to spawn Brave executable at %s: %s", cp, e)


async def apply_workspace_preset(preset_name: str) -> tuple[bool, str]:
    """Apply named multi-application desktop workspace arrangement."""
    clean = preset_name.strip().lower()

    if clean in ("coding", "dev"):
        from orion.tools import app_launcher

        # Check / Launch VS Code
        hwnd_code, _ = find_target_window("code")
        if not hwnd_code:
            app_launcher.launch_application("vscode")

        # Check / Launch Brave
        _launch_brave_if_needed()

        # Poll briefly for windows to surface if newly spawned
        for _ in range(6):
            hwnd_code, _ = find_target_window("code")
            hwnd_brave, _ = find_target_window("brave")
            if hwnd_code and hwnd_brave:
                break
            await asyncio.sleep(0.5)

        s1, m1 = tile_window("code", "left")
        s2, m2 = tile_window("brave", "right")
        if not s1 and not s2:
            return False, "⚠️ Could not arrange coding workspace (neither VS Code nor Brave window found)."

        return True, (
            "🪟 *Workspace Preset 'coding' Applied:*\n"
            "• *VS Code* snapped to *left* (50%)\n"
            "• *Brave* snapped to *right* (50%)"
        )

    elif clean in ("research", "study"):
        from orion.tools import app_launcher

        # Check / Launch Brave
        _launch_brave_if_needed()

        hwnd_notepad, _ = find_target_window("notepad")
        if not hwnd_notepad:
            app_launcher.launch_application("notepad")

        for _ in range(6):
            hwnd_brave, _ = find_target_window("brave")
            hwnd_notepad, _ = find_target_window("notepad")
            if hwnd_brave and hwnd_notepad:
                break
            await asyncio.sleep(0.5)

        s1, m1 = tile_window("brave", "left")
        s2, m2 = tile_window("notepad", "right")
        if not s1 and not s2:
            return False, "⚠️ Could not arrange research workspace (neither Brave nor Notepad window found)."

        return True, (
            "🪟 *Workspace Preset 'research' Applied:*\n"
            "• *Brave* snapped to *left* (50%)\n"
            "• *Notepad* snapped to *right* (50%)"
        )

    elif clean in ("focus", "minimal"):
        # Maximize active window or VS Code if open
        target = "code" if find_target_window("code")[0] else "active"
        success, msg = tile_window(target, "maximize")
        if success:
            return True, f"🪟 *Workspace Preset 'focus' Applied:*\n• Maximized *{target}* for distraction-free focus."
        return False, f"⚠️ Failed to apply focus preset: {msg}"

    return False, f"⚠️ Unknown workspace preset *'{preset_name}'*. Available presets: `coding`, `dev`, `research`, `study`, `focus`."
