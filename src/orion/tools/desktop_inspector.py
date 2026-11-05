"""Safe Windows desktop window inspector and controlled window manager for ORION (Steps 10–11).

Strictly enforces:
- Read-only window discovery, active window detection, and state inspection (Step 10).
- Controlled window focus/minimize/maximize/restore with confirmation and re-validation (Step 11).
- Reuse of the exact 20-app allowlist from Step 9 (no secondary allowlists).
- No screenshots, OCR, screen pixels, window text, clipboard, or keystroke capture.
- Process paths, PIDs, and window handles remain internal and are never leaked.
- Unapproved applications and system shell windows are filtered out completely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import logging
import os
from pathlib import Path
import sys
import time
from typing import Any, ClassVar, Literal

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


# =====================================================================
# STEP 11: CONTROLLED WINDOW MANAGEMENT
# =====================================================================

# SW constants for ShowWindow / ShowWindowAsync
_SW_MINIMIZE = 6
_SW_MAXIMIZE = 3
_SW_RESTORE = 9
_SW_SHOW = 5

# Window management action types
_WINDOW_MANAGEMENT_TOOLS = frozenset({
    "focus_application_window",
    "minimize_application_window",
    "maximize_application_window",
    "restore_application_window",
})

# Short-lived window token store (module-level)
_window_tokens: dict[str, "WindowToken"] = {}


@dataclass
class WindowToken:
    """Short-lived opaque window selection token bound to verified identity."""

    app_id: str
    hwnd: int               # Only used internally for quick lookup; re-validated at execution
    pid: int                # Captured PID for identity verification
    state: WindowState
    label: str              # e.g. "Visual Studio Code — Window 1"
    created_at: float       # time.time()

    TOKEN_TTL_SECONDS: ClassVar[float] = 300.0  # 5 minutes

    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > self.TOKEN_TTL_SECONDS


def _make_token_id() -> str:
    """Generate an 8-char hex token ID using high-entropy random bytes."""
    import os as _os
    return _os.urandom(4).hex()


def _purge_expired_tokens() -> None:
    """Remove expired entries from the global token store."""
    expired = [tid for tid, tok in _window_tokens.items() if tok.is_expired()]
    for tid in expired:
        _window_tokens.pop(tid, None)


def create_window_tokens(app_id: str, windows: list[WindowInfo]) -> list[dict]:
    """Create labeled selection options with short-lived tokens for multi-window apps."""
    _purge_expired_tokens()
    options = []
    for idx, win in enumerate(windows, start=1):
        token_id = _make_token_id()
        label = f"{win.display_name} \u2014 Window {idx}"
        tok = WindowToken(
            app_id=app_id,
            hwnd=win.hwnd,
            pid=getattr(win, "_pid", 0),
            state=win.state,
            label=label,
            created_at=time.time(),
        )
        _window_tokens[token_id] = tok
        options.append({
            "token_id": token_id,
            "label": label,
            "state": win.state,
        })
    return options


def resolve_window_token(token_id: str, app_id: str) -> tuple[WindowInfo | None, str | None]:
    """Resolve a token to a re-verified WindowInfo, or return error message.

    Returns (WindowInfo, None) on success or (None, error_message) on failure.
    """
    _purge_expired_tokens()

    tok = _window_tokens.get(token_id)
    if not tok:
        return None, "Window selection token not found or has expired."

    if tok.app_id != app_id:
        return None, "Window selection token does not match the requested application."

    if tok.is_expired():
        _window_tokens.pop(token_id, None)
        return None, "Window selection token has expired. Please retry the operation."

    # Re-enumerate windows to find one matching both hwnd and pid
    try:
        all_windows = _enumerate_windows_native()
    except Exception as err:
        logger.error("Failed to re-enumerate windows during token resolution: %s", err)
        return None, "Desktop inspection temporarily unavailable."

    # Verify hwnd is still valid
    if sys.platform == "win32":
        import ctypes
        user32 = ctypes.windll.user32
        if not user32.IsWindow(tok.hwnd):
            _window_tokens.pop(token_id, None)
            return None, "The target window is no longer available."

    # Find window with matching hwnd AND app_id; verify PID if stored
    for win in all_windows:
        if win.hwnd == tok.hwnd and win.app_id == app_id:
            if tok.pid:
                pid, _ = _get_real_window_process_info(win.hwnd)
                if pid != tok.pid:
                    _window_tokens.pop(token_id, None)
                    return None, "The target window process identity has changed."
            return win, None

    _window_tokens.pop(token_id, None)
    return None, "The target window is no longer available."


def _get_single_app_window(app_id: str) -> tuple[WindowInfo | None, list[WindowInfo], str | None]:
    """Return (single_window_or_None, all_windows_for_app, error_message_or_None).

    If there's exactly one window → (win, [win], None).
    If there are multiple → (None, [win1, win2, ...], None).
    If there are none → (None, [], error_message).
    """
    try:
        all_windows = _enumerate_windows_native()
    except Exception as err:
        logger.error("Failed to enumerate windows for %s: %s", app_id, err)
        return None, [], "Desktop inspection temporarily unavailable."

    app_windows = [w for w in all_windows if w.app_id == app_id]
    if not app_windows:
        approved = get_approved_apps()
        disp = approved[app_id].display_name if app_id in approved else app_id
        return None, [], f"{disp} does not have any open windows."

    if len(app_windows) == 1:
        return app_windows[0], app_windows, None

    return None, app_windows, None


def _verify_and_act_on_window(
    hwnd: int,
    app_id: str,
    action: str,
) -> dict[str, Any]:
    """Execute a ShowWindow(Async)/SetForegroundWindow call and verify the result.

    Args:
        hwnd: validated window handle
        app_id: approved app_id for the window
        action: one of "focus", "minimize", "maximize", "restore"

    Returns a dict with at least {"success": bool, ...}.
    """
    if sys.platform != "win32":
        return {"success": False, "error": "Window management is only available on Windows."}

    import ctypes
    user32 = ctypes.windll.user32

    approved = get_approved_apps()
    disp = approved[app_id].display_name if app_id in approved else app_id

    if action == "focus":
        # If minimized, restore first so it can receive focus
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, _SW_RESTORE)

        result = user32.SetForegroundWindow(hwnd)
        fg = user32.GetForegroundWindow()

        if fg == hwnd:
            return {
                "success": True,
                "app_id": app_id,
                "display_name": disp,
                "message": f"{disp} is now in the foreground.",
            }
        else:
            return {
                "success": False,
                "app_id": app_id,
                "display_name": disp,
                "error": "Windows prevented the focus change.",
            }

    elif action == "minimize":
        if user32.IsIconic(hwnd):
            return {
                "success": True,
                "app_id": app_id,
                "display_name": disp,
                "message": f"{disp} is already minimized.",
                "note": "already_in_state",
            }

        user32.ShowWindowAsync(hwnd, _SW_MINIMIZE)
        # Poll up to 5 times × 60ms = 300ms
        for _ in range(5):
            time.sleep(0.06)
            if user32.IsIconic(hwnd):
                return {
                    "success": True,
                    "app_id": app_id,
                    "display_name": disp,
                    "message": f"{disp} has been minimized.",
                }
        return {
            "success": False,
            "app_id": app_id,
            "display_name": disp,
            "error": f"Failed to verify that {disp} was minimized.",
        }

    elif action == "maximize":
        if user32.IsZoomed(hwnd) and not user32.IsIconic(hwnd):
            return {
                "success": True,
                "app_id": app_id,
                "display_name": disp,
                "message": f"{disp} is already maximized.",
                "note": "already_in_state",
            }

        user32.ShowWindowAsync(hwnd, _SW_MAXIMIZE)
        for _ in range(5):
            time.sleep(0.06)
            if user32.IsZoomed(hwnd) and not user32.IsIconic(hwnd):
                return {
                    "success": True,
                    "app_id": app_id,
                    "display_name": disp,
                    "message": f"{disp} has been maximized.",
                }
        return {
            "success": False,
            "app_id": app_id,
            "display_name": disp,
            "error": f"Failed to verify that {disp} was maximized.",
        }

    elif action == "restore":
        if not user32.IsIconic(hwnd) and not user32.IsZoomed(hwnd):
            return {
                "success": True,
                "app_id": app_id,
                "display_name": disp,
                "message": f"{disp} is already in Normal state.",
                "note": "already_in_state",
            }

        user32.ShowWindowAsync(hwnd, _SW_RESTORE)
        for _ in range(5):
            time.sleep(0.06)
            if not user32.IsIconic(hwnd) and not user32.IsZoomed(hwnd):
                return {
                    "success": True,
                    "app_id": app_id,
                    "display_name": disp,
                    "message": f"{disp} has been restored to Normal.",
                }
        return {
            "success": False,
            "app_id": app_id,
            "display_name": disp,
            "error": f"Failed to verify that {disp} was restored to Normal state.",
        }

    return {"success": False, "error": f"Unknown window action: {action}"}


def _window_management_core(
    app_id: str,
    window_token: str | None,
    action: str,
) -> dict[str, Any]:
    """Shared implementation for all 4 window management tools.

    Enforces:
    - app_id is in the approved allowlist
    - Platform is Windows
    - Window re-enumeration and identity re-validation at execution time
    - Token expiry and stale-window detection
    """
    if sys.platform != "win32":
        return {"success": False, "error": "Window management is only available on Windows."}

    resolved_id = resolve_app_alias(app_id)
    approved = get_approved_apps()

    if not resolved_id or resolved_id not in approved:
        return {
            "success": False,
            "error": f"Application '{app_id}' is not in the approved application allowlist.",
        }

    disp = approved[resolved_id].display_name

    # Token-based path: resolve token to a specific window
    if window_token:
        win, err = resolve_window_token(window_token, resolved_id)
        if err:
            return {"success": False, "error": err}
        if win is None:
            return {"success": False, "error": "The target window is no longer available."}
        return _verify_and_act_on_window(win.hwnd, resolved_id, action)

    # No token: enumerate and find target window(s)
    single_win, app_windows, enum_err = _get_single_app_window(resolved_id)

    if enum_err:
        return {"success": False, "error": enum_err}

    if not app_windows:
        return {"success": False, "error": f"{disp} does not have any open windows."}

    # Multiple windows — need selection
    if len(app_windows) > 1:
        options = create_window_tokens(resolved_id, app_windows)
        return {
            "success": False,
            "requires_selection": True,
            "app_id": resolved_id,
            "display_name": disp,
            "windows": options,
        }

    # Exactly one window
    return _verify_and_act_on_window(single_win.hwnd, resolved_id, action)


# =====================================================================
# PUBLIC WINDOW MANAGEMENT TOOLS (Step 11)
# =====================================================================

def focus_application_window(app_id: str, window_token: str | None = None) -> dict[str, Any]:
    """Bring an approved application window to the foreground.

    Requires confirmed re-enumeration before acting. Reports accurate success/failure.
    Never operates on unapproved applications or leaks process details.
    """
    return _window_management_core(app_id, window_token, "focus")


def minimize_application_window(app_id: str, window_token: str | None = None) -> dict[str, Any]:
    """Minimize an approved application window. Verifies state afterward."""
    return _window_management_core(app_id, window_token, "minimize")


def maximize_application_window(app_id: str, window_token: str | None = None) -> dict[str, Any]:
    """Maximize an approved application window. Verifies state afterward."""
    return _window_management_core(app_id, window_token, "maximize")


def restore_application_window(app_id: str, window_token: str | None = None) -> dict[str, Any]:
    """Restore a minimized/maximized approved application window to Normal state.

    Verifies state afterward. Never claims success if the final state does not confirm.
    """
    return _window_management_core(app_id, window_token, "restore")
