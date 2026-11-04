"""Safe, read-only Windows desktop window inspector for ORION (Step 10).

Strictly enforces:
- Read-only window discovery, active window detection, and state inspection.
- Reuse of the exact 20-app allowlist from Step 9 (no secondary allowlists).
- No desktop automation or control (no focus changes, resizing, or closing).
- No screenshots, OCR, screen pixels, window text, clipboard, or keystroke capture.
- Process paths, PIDs, and window handles remain internal and are never leaked.
- Unapproved applications and system shell windows are filtered out completely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import sys
from typing import Any, Literal

from orion.tools.app_launcher import (
    APPROVED_APPLICATIONS,
    get_approved_apps,
    resolve_app_alias,
)

logger = logging.getLogger("orion.tools.desktop_inspector")

WindowState = Literal["Normal", "Minimized", "Maximized"]


@dataclass
class WindowInfo:
    """Internal metadata for a verified window."""

    hwnd: int
    app_id: str
    display_name: str
    state: WindowState
    is_active: bool = False


def _match_process_to_approved_app(proc_path: str, win_class: str) -> str | None:
    """Safely match a process executable path and window class to an approved app_id.

    Rejects all unapproved applications, impostors, and generic shell windows.
    Returns the approved app_id or None.
    """
    if not proc_path:
        return None

    path_lower = proc_path.replace("/", "\\").lower()
    base_name = os.path.basename(path_lower)

    # 1. File Explorer vs Windows Shell infrastructure
    if base_name == "explorer.exe":
        # Only true File Explorer windows (CabinetWClass, ExploreWClass) are matched
        if win_class in ("CabinetWClass", "ExploreWClass"):
            return "file_explorer"
        return None

    # 2. Win32 Approved Applications (verify expected directory context to reject impostors)
    if "microsoft office" in path_lower or "root\\office" in path_lower:
        if base_name == "winword.exe":
            return "word"
        if base_name == "excel.exe":
            return "excel"
        if base_name == "powerpnt.exe":
            return "powerpoint"

    if base_name == "notepad.exe":
        return "notepad"

    if "microsoft vs code" in path_lower and base_name == "code.exe":
        return "vscode"

    if "antigravity" in path_lower and base_name in ("antigravity ide.exe", "antigravity.exe"):
        return "antigravity_ide"

    if "microsoft\\edge" in path_lower and base_name == "msedge.exe":
        return "edge"

    if "comet" in path_lower and base_name == "comet.exe":
        return "comet"

    if "zoom" in path_lower and base_name == "zoom.exe":
        return "zoom"

    if "videolan" in path_lower and base_name == "vlc.exe":
        return "vlc"

    if "winrar" in path_lower and base_name == "winrar.exe":
        return "winrar"

    # 3. Packaged Approved Applications (UWP / MSIX)
    if "openai.codex" in path_lower and base_name in ("chatgpt.exe", "openai.codex.exe"):
        return "chatgpt"

    if "claude" in path_lower and base_name == "claude.exe":
        return "claude"

    if "5319275a.whatsappdesktop" in path_lower and base_name == "whatsapp.exe":
        return "whatsapp"

    if "msteams" in path_lower and base_name in ("ms-teams.exe", "msteams.exe"):
        return "teams"

    if "microsoft.windowscalculator" in path_lower or base_name == "calculatorapp.exe":
        return "calculator"

    if (
        "microsoft.microsoftstickynotes" in path_lower
        or "onenote.memorypreview" in path_lower
        or "microsoft.officenote" in path_lower
    ) and base_name in ("stickynotes.exe", "onenote.exe"):
        return "sticky_notes"

    if "microsoft.paint" in path_lower and base_name == "mspaint.exe":
        return "paint"

    if "microsoft.screensketch" in path_lower and base_name in ("snippingtool.exe", "screensketch.exe"):
        return "snipping_tool"

    return None


def _get_process_path_native(pid: int) -> str:
    """Retrieve process executable path via native Windows QueryFullProcessImageName."""
    if sys.platform != "win32" or pid <= 0:
        return ""

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h_proc:
        return ""

    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
            return buf.value
    except Exception as err:
        logger.debug("Error querying process image for PID %d: %s", pid, err)
    finally:
        kernel32.CloseHandle(h_proc)

    return ""


def _inspect_window_state_native(hwnd: int) -> WindowState:
    """Detect whether a window is Normal, Minimized, or Maximized."""
    if sys.platform != "win32":
        return "Normal"

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    # 1. Direct IsIconic / IsZoomed checks
    if user32.IsIconic(hwnd):
        return "Minimized"
    if user32.IsZoomed(hwnd):
        return "Maximized"

    # 2. GetWindowPlacement check
    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    class WINDOWPLACEMENT(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.UINT),
            ("flags", wintypes.UINT),
            ("showCmd", wintypes.UINT),
            ("ptMinPosition", POINT),
            ("ptMaxPosition", POINT),
            ("rcNormalPosition", RECT),
        ]

    wp = WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(WINDOWPLACEMENT)
    if user32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
        # SW_SHOWMINIMIZED=2, SW_MINIMIZE=6, SW_SHOWMINNOACTIVE=7, SW_FORCEMINIMIZE=11
        if wp.showCmd in (2, 6, 7, 11):
            return "Minimized"
        if wp.showCmd == 3:
            return "Maximized"

    return "Normal"


def _get_real_window_process_info(hwnd: int) -> tuple[int, str]:
    """Resolve the real PID and executable path for a window, unwrapping ApplicationFrameHost if needed."""
    if sys.platform != "win32":
        return 0, ""

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return 0, ""

    path = _get_process_path_native(pid.value)
    base = os.path.basename(path).lower()

    # Unwrap ApplicationFrameHost to discover hosted packaged child process
    if base == "applicationframehost.exe":
        child_pids: list[int] = []

        def enum_child(child_hwnd: int, lparam: int) -> bool:
            cpid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(child_hwnd, ctypes.byref(cpid))
            if cpid.value and cpid.value != pid.value:
                child_pids.append(cpid.value)
            return True

        ChildEnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        user32.EnumChildWindows(hwnd, ChildEnumProc(enum_child), 0)

        for cpid in child_pids:
            cpath = _get_process_path_native(cpid)
            if cpath:
                return cpid, cpath

    return pid.value, path


def _enumerate_windows_native() -> list[WindowInfo]:
    """Enumerate desktop top-level windows and map them to approved applications."""
    if sys.platform != "win32":
        return []

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    dwmapi = ctypes.windll.dwmapi

    DWMWA_CLOAKED = 14
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_APPWINDOW = 0x00040000

    GetWindowLongPtrW = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)

    # 1. Get active foreground window handle
    fg_hwnd = _get_foreground_hwnd_native()

    # 2. Enumerate windows
    results: list[WindowInfo] = []
    approved = get_approved_apps()

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def window_enum_callback(hwnd: int, lparam: int) -> bool:
        try:
            if not user32.IsWindowVisible(hwnd):
                return True

            # Filter cloaked windows (hidden by DWM/Shell)
            cloaked = wintypes.DWORD()
            if (
                dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0
                and cloaked.value != 0
            ):
                return True

            # Filter tool windows without app window style
            ex_style = GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
            if (ex_style & WS_EX_TOOLWINDOW) and not (ex_style & WS_EX_APPWINDOW):
                return True

            # Get window class name
            class_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_buf, 256)
            win_class = class_buf.value

            # Filter background shell / desktop windows
            if win_class in ("Progman", "Shell_TrayWnd", "WorkerW", "Xaml_WindowedPopupClass"):
                return True

            pid, proc_path = _get_real_window_process_info(hwnd)
            if not proc_path:
                return True

            app_id = _match_process_to_approved_app(proc_path, win_class)
            if not app_id or app_id not in approved:
                return True

            state = _inspect_window_state_native(hwnd)
            is_active = (hwnd == fg_hwnd)

            results.append(
                WindowInfo(
                    hwnd=hwnd,
                    app_id=app_id,
                    display_name=approved[app_id].display_name,
                    state=state,
                    is_active=is_active,
                )
            )
        except Exception as err:
            logger.debug("Exception inspecting hwnd %s: %s", hwnd, err)

        return True

    cb_func = WNDENUMPROC(window_enum_callback)

    # Try interactive desktop enumeration first, fall back to EnumWindows
    h_desk = None
    try:
        DESKTOP_ENUMERATE = 0x0040
        DESKTOP_READOBJECTS = 0x0001
        h_desk = user32.OpenDesktopW("default", 0, False, DESKTOP_ENUMERATE | DESKTOP_READOBJECTS)
        if h_desk:
            user32.EnumDesktopWindows(h_desk, cb_func, 0)
        else:
            user32.EnumWindows(cb_func, 0)
    except Exception as err:
        logger.debug("Desktop enumeration exception: %s", err)
        try:
            user32.EnumWindows(cb_func, 0)
        except Exception:
            pass
    finally:
        if h_desk:
            try:
                user32.CloseDesktop(h_desk)
            except Exception:
                pass

    return results


def _get_foreground_hwnd_native() -> int:
    """Retrieve the handle of the foreground desktop window, handling thread desktop context."""
    if sys.platform != "win32":
        return 0

    import ctypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    # Check foreground directly
    fg = user32.GetForegroundWindow()
    if fg:
        return fg

    # Switch thread desktop to default if necessary
    h_orig_desk = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
    h_default_desk = None
    try:
        h_default_desk = user32.OpenDesktopW("default", 0, False, 0x01FF)
        if h_default_desk:
            user32.SetThreadDesktop(h_default_desk)
            fg = user32.GetForegroundWindow()
    except Exception:
        pass
    finally:
        if h_default_desk:
            try:
                if h_orig_desk:
                    user32.SetThreadDesktop(h_orig_desk)
                user32.CloseDesktop(h_default_desk)
            except Exception:
                pass

    return fg or 0


# =====================================================================
# PUBLIC READ-ONLY DESKTOP TOOLS (Step 10)
# =====================================================================

def list_open_windows() -> dict[str, Any]:
    """Retrieve list of approved applications that currently have open desktop windows.

    Groups multiple windows by application and identifies window states (Normal, Minimized, Maximized).
    Never exposes raw window titles, process paths, PIDs, or unapproved applications.
    """
    try:
        windows = _enumerate_windows_native()
    except Exception as err:
        logger.error("Failed to inspect desktop windows: %s", err)
        return {
            "success": False,
            "error": "Desktop inspection is currently unavailable on this system.",
            "open_windows": [],
            "total_windows": 0,
        }

    # Group windows by approved application
    grouped: dict[str, dict[str, Any]] = {}
    for win in windows:
        if win.app_id not in grouped:
            grouped[win.app_id] = {
                "app_id": win.app_id,
                "display_name": win.display_name,
                "window_count": 0,
                "states": [],
                "is_active": False,
            }
        grouped[win.app_id]["window_count"] += 1
        grouped[win.app_id]["states"].append(win.state)
        if win.is_active:
            grouped[win.app_id]["is_active"] = True

    app_list = list(grouped.values())
    app_list.sort(key=lambda x: x["display_name"].lower())
    total = sum(x["window_count"] for x in app_list)

    if not app_list:
        return {
            "success": True,
            "open_windows": [],
            "total_windows": 0,
            "message": "No approved applications currently have open windows.",
        }

    return {
        "success": True,
        "open_windows": app_list,
        "total_windows": total,
        "message": f"Found {len(app_list)} approved application(s) with open windows.",
    }


def get_active_window() -> dict[str, Any]:
    """Identify which approved application is currently in the foreground.

    Returns friendly notice if the foreground window is unapproved, unidentifiable, or desktop/lock screen.
    Never leaks process paths or details of unapproved applications.
    """
    try:
        fg_hwnd = _get_foreground_hwnd_native()
        if not fg_hwnd:
            return {
                "success": True,
                "has_active_approved_window": False,
                "message": "The active application is not available to ORION.",
            }

        pid, proc_path = _get_real_window_process_info(fg_hwnd)
        if not proc_path:
            return {
                "success": True,
                "has_active_approved_window": False,
                "message": "The active application is not available to ORION.",
            }

        import ctypes
        user32 = ctypes.windll.user32 if sys.platform == "win32" else None
        win_class = ""
        if user32:
            class_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(fg_hwnd, class_buf, 256)
            win_class = class_buf.value

        app_id = _match_process_to_approved_app(proc_path, win_class)
        approved = get_approved_apps()
        if not app_id or app_id not in approved:
            return {
                "success": True,
                "has_active_approved_window": False,
                "message": "The active application is not available to ORION.",
            }

        state = _inspect_window_state_native(fg_hwnd)
        disp_name = approved[app_id].display_name

        return {
            "success": True,
            "has_active_approved_window": True,
            "app_id": app_id,
            "display_name": disp_name,
            "state": state,
            "message": f"Active application: **{disp_name}** ({state}).",
        }
    except Exception as err:
        logger.error("Error retrieving active window: %s", err)
        return {
            "success": False,
            "error": "Desktop inspection is currently unavailable on this system.",
            "has_active_approved_window": False,
            "message": "The active application is not available to ORION.",
        }


def get_application_window_status(app_id: str) -> dict[str, Any]:
    """Check window status for a specific approved application.

    Returns whether it has open windows, window count, states, and active status.
    Rejects unapproved app IDs.
    """
    resolved_id = resolve_app_alias(app_id)
    approved = get_approved_apps()

    if not resolved_id or resolved_id not in approved:
        return {
            "success": False,
            "app_id": app_id,
            "error": f"Application '{app_id}' is not in the approved application allowlist.",
        }

    app = approved[resolved_id]

    try:
        windows = _enumerate_windows_native()
    except Exception as err:
        logger.error("Failed to inspect windows for %s: %s", app.display_name, err)
        return {
            "success": False,
            "app_id": resolved_id,
            "display_name": app.display_name,
            "error": "Desktop inspection is currently unavailable on this system.",
        }

    app_windows = [w for w in windows if w.app_id == resolved_id]
    count = len(app_windows)
    states = [w.state for w in app_windows]
    is_active = any(w.is_active for w in app_windows)

    if count == 0:
        return {
            "success": True,
            "app_id": resolved_id,
            "display_name": app.display_name,
            "has_open_window": False,
            "window_count": 0,
            "states": [],
            "is_active": False,
            "message": f"**{app.display_name}** does not currently have any open windows.",
        }

    # Format state summary: e.g. "1 Normal, 1 Minimized"
    state_counts: dict[str, int] = {}
    for s in states:
        state_counts[s] = state_counts.get(s, 0) + 1
    state_str = ", ".join(f"{cnt} {st}" for st, cnt in state_counts.items())
    active_tag = " [Active]" if is_active else ""

    return {
        "success": True,
        "app_id": resolved_id,
        "display_name": app.display_name,
        "has_open_window": True,
        "window_count": count,
        "states": states,
        "is_active": is_active,
        "message": f"**{app.display_name}** has {count} open window(s) ({state_str}){active_tag}.",
    }
