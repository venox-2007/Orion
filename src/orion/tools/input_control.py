"""Virtual input and remote clipboard management for ORION (Step 25)."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
from typing import Any

logger = logging.getLogger("orion.tools.input_control")

# Virtual key codes for common navigation keys
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
VK_TAB = 0x09
VK_SPACE = 0x20
VK_BACK = 0x08

SPECIAL_KEYS: dict[str, int] = {
    "enter": VK_RETURN,
    "return": VK_RETURN,
    "escape": VK_ESCAPE,
    "esc": VK_ESCAPE,
    "tab": VK_TAB,
    "space": VK_SPACE,
    "backspace": VK_BACK,
}

# Input event constants
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("union", _INPUT_UNION),
    ]


def _setup_ctypes_signatures() -> tuple[Any, Any]:
    """Configure ctypes Windows API argument and return types."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]

    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]

    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]

    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardData.argtypes = [ctypes.c_uint]

    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT

    return user32, kernel32


def get_clipboard_text() -> str:
    """Read plain text from the Windows clipboard."""
    try:
        import pyperclip

        return pyperclip.paste() or ""
    except Exception:
        pass

    try:
        user32, kernel32 = _setup_ctypes_signatures()

        if not user32.OpenClipboard(None):
            return ""
        try:
            h_data = user32.GetClipboardData(CF_UNICODETEXT)
            if not h_data:
                return ""
            ptr = kernel32.GlobalLock(h_data)
            if not ptr:
                return ""
            try:
                return str(ctypes.wstring_at(ptr))
            finally:
                kernel32.GlobalUnlock(h_data)
        finally:
            user32.CloseClipboard()
    except Exception as e:
        logger.debug("Failed to read clipboard: %s", e)
        return ""


def set_clipboard_text(text: str) -> bool:
    """Set the Windows clipboard content to text."""
    try:
        import pyperclip

        pyperclip.copy(text)
        return True
    except Exception:
        pass

    try:
        user32, kernel32 = _setup_ctypes_signatures()

        if not user32.OpenClipboard(None):
            return False
        try:
            user32.EmptyClipboard()
            encoded = text.encode("utf-16le") + b"\x00\x00"
            h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
            if not h_mem:
                return False
            ptr = kernel32.GlobalLock(h_mem)
            if not ptr:
                kernel32.GlobalFree(h_mem)
                return False
            try:
                ctypes.memmove(ptr, encoded, len(encoded))
            finally:
                kernel32.GlobalUnlock(h_mem)
            user32.SetClipboardData(CF_UNICODETEXT, h_mem)
            return True
        finally:
            user32.CloseClipboard()
    except Exception as e:
        logger.debug("Failed to set clipboard: %s", e)
        return False


def type_text(text: str) -> bool:
    """Inject text character-by-character into the active window using SendInput with KEYEVENTF_UNICODE."""
    if not text:
        return True

    try:
        user32, _ = _setup_ctypes_signatures()
        for char in text:
            code = ord(char)
            inp_down = INPUT(
                type=INPUT_KEYBOARD,
                union=_INPUT_UNION(
                    ki=KEYBDINPUT(
                        wVk=0,
                        wScan=code,
                        dwFlags=KEYEVENTF_UNICODE,
                        time=0,
                        dwExtraInfo=0,
                    )
                ),
            )
            inp_up = INPUT(
                type=INPUT_KEYBOARD,
                union=_INPUT_UNION(
                    ki=KEYBDINPUT(
                        wVk=0,
                        wScan=code,
                        dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
                        time=0,
                        dwExtraInfo=0,
                    )
                ),
            )
            inputs = (INPUT * 2)(inp_down, inp_up)
            user32.SendInput(2, inputs, ctypes.sizeof(INPUT))
        return True
    except Exception as e:
        logger.warning("Failed to type text via SendInput: %s", e)
        return False


def press_special_key(key_name: str) -> bool:
    """Press and release a navigation key in the active window."""
    clean_key = key_name.strip().lower()
    vk = SPECIAL_KEYS.get(clean_key)
    if vk is None:
        return False

    try:
        user32, _ = _setup_ctypes_signatures()
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        return True
    except Exception as e:
        logger.warning("Failed to press special key %s: %s", key_name, e)
        return False
