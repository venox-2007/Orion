"""Virtual input and remote clipboard management for ORION (Step 25)."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
from typing import Any

logger = logging.getLogger("orion.tools.input_control")

# Persistent active target window context across remote input commands
_last_target_app: str | None = None


def get_last_target_app() -> str | None:
    """Return the name of the last focused target application, or None."""
    return _last_target_app


def set_last_target_app(app_name: str | None) -> None:
    """Set or clear the last focused target application."""
    global _last_target_app
    _last_target_app = app_name

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


def type_text(text: str, target_app: str | None = None) -> bool:
    """Inject text character-by-character into the active window using SendInput with KEYEVENTF_UNICODE."""
    if not text:
        return True

    if target_app:
        if not focus_window_by_name(target_app):
            return False
    elif _last_target_app:
        focus_window_by_name(_last_target_app)

    try:
        user32, _ = _setup_ctypes_signatures()
        import time

        for char in text:
            if char == "\r":
                continue
            if char == "\n":
                inp_down = INPUT(
                    type=INPUT_KEYBOARD,
                    union=_INPUT_UNION(
                        ki=KEYBDINPUT(
                            wVk=VK_RETURN,
                            wScan=0,
                            dwFlags=0,
                            time=0,
                            dwExtraInfo=0,
                        )
                    ),
                )
                inp_up = INPUT(
                    type=INPUT_KEYBOARD,
                    union=_INPUT_UNION(
                        ki=KEYBDINPUT(
                            wVk=VK_RETURN,
                            wScan=0,
                            dwFlags=KEYEVENTF_KEYUP,
                            time=0,
                            dwExtraInfo=0,
                        )
                    ),
                )
            else:
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
            time.sleep(0.008)
        return True
    except Exception as e:
        logger.warning("Failed to type text via SendInput: %s", e)
        return False


def press_special_key(key_name: str, target_app: str | None = None) -> bool:
    """Press and release a navigation or alphanumeric key in the active window."""
    clean_key = key_name.strip().lower()
    vk = SPECIAL_KEYS.get(clean_key)
    if vk is None and len(clean_key) == 1 and clean_key.isalnum():
        vk = ord(clean_key.upper())
    if vk is None:
        return False

    if target_app:
        if not focus_window_by_name(target_app):
            return False
    elif _last_target_app:
        focus_window_by_name(_last_target_app)

    try:
        user32, _ = _setup_ctypes_signatures()
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        return True
    except Exception as e:
        logger.warning("Failed to press special key %s: %s", key_name, e)
        return False


def force_foreground_window(hwnd: int) -> bool:
    """Foolproof foreground window activation bypassing Windows 11 thread restrictions."""
    if not hwnd:
        return False

    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # 1. Check if already active
        if user32.GetForegroundWindow() == hwnd:
            return True

        # 2. Restore if minimized
        SW_RESTORE = 9
        SW_SHOW = 5
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        else:
            user32.ShowWindow(hwnd, SW_SHOW)

        # 3. Bypass Windows 11 foreground lock using AttachThreadInput
        fg_hwnd = user32.GetForegroundWindow()
        fg_thread = user32.GetWindowThreadProcessId(fg_hwnd, None)
        cur_thread = kernel32.GetCurrentThreadId()

        if fg_thread and fg_thread != cur_thread:
            user32.AttachThreadInput(cur_thread, fg_thread, True)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.AttachThreadInput(cur_thread, fg_thread, False)
        else:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)

        # 4. Settle & verify: poll for up to 0.5s until user32.GetForegroundWindow() == hwnd
        import time

        for _ in range(10):
            time.sleep(0.05)
            if user32.GetForegroundWindow() == hwnd:
                return True

        return user32.GetForegroundWindow() == hwnd
    except Exception as e:
        logger.warning("Failed to force foreground window (%s): %s", hwnd, e)
        return False


def focus_window_by_name(target_name: str) -> bool:
    """Find and bring to foreground an active visible window matching target_name."""
    clean_target = target_name.strip().lower()
    if not clean_target:
        return False

    name_mappings = {
        "notepad": ["notepad"],
        "chrome": ["chrome", "google chrome"],
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
    match_keywords = name_mappings.get(clean_target, [clean_target])

    try:
        user32 = ctypes.windll.user32
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        found_hwnd = None

        def enum_cb(hwnd, lparam):
            nonlocal found_hwnd
            if hasattr(user32, "IsWindowVisible") and not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buff = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buff, length + 1)
                title = buff.value.lower()
                for kw in match_keywords:
                    if kw in title:
                        found_hwnd = hwnd
                        return False
            return True

        cb_func = WNDENUMPROC(enum_cb)

        # 1. Try EnumWindows first
        user32.EnumWindows(cb_func, 0)

        # 2. If not found, try desktop enumeration (for interactive Windows sessions)
        if not found_hwnd and hasattr(user32, "OpenDesktopW"):
            try:
                DESKTOP_ENUMERATE = 0x0040
                DESKTOP_READOBJECTS = 0x0001
                h_desk = user32.OpenDesktopW("default", 0, False, DESKTOP_ENUMERATE | DESKTOP_READOBJECTS)
                if h_desk:
                    try:
                        user32.EnumDesktopWindows(h_desk, cb_func, 0)
                    finally:
                        user32.CloseDesktop(h_desk)
            except Exception:
                pass

        # 3. Fallback: check approved apps from desktop_inspector if available
        if not found_hwnd:
            try:
                from orion.tools.desktop_inspector import _enumerate_windows_native

                wins = _enumerate_windows_native()
                for w in wins:
                    for kw in match_keywords:
                        if kw in w.app_id.lower() or kw in w.display_name.lower():
                            found_hwnd = w.hwnd
                            break
                    if found_hwnd:
                        break
            except Exception:
                pass

        if not found_hwnd:
            return False

        success = force_foreground_window(found_hwnd)
        if success:
            set_last_target_app(clean_target)
        return success
    except Exception as e:
        logger.warning("Failed to focus window for %s: %s", target_name, e)
        return False
