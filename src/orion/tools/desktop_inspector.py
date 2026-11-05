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
import re
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
_pending_window_selections: dict[str, dict[int, str]] = {}
_latest_pending_selection: dict[str, Any] | None = None


@dataclass
class WindowToken:
    """Short-lived opaque window selection token bound to verified identity."""

    app_id: str
    hwnd: int               # Only used internally for quick lookup; re-validated at execution
    pid: int                # Captured PID for identity verification
    state: WindowState
    label: str              # e.g. "Visual Studio Code — Main window"
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


def create_window_tokens(
    app_id: str,
    windows: list[WindowInfo],
    action: str = "focus",
) -> list[dict]:
    """Create labeled selection options with short-lived tokens for multi-window apps."""
    _purge_expired_tokens()
    global _latest_pending_selection
    options = []
    total = len(windows)
    app_tokens: dict[int, str] = {}
    for idx, win in enumerate(windows, start=1):
        token_id = _make_token_id()
        if total == 2:
            label_suffix = "Main window" if idx == 1 else "Secondary window"
        else:
            label_suffix = f"Window {idx}"
        label = f"{win.display_name} — {label_suffix}"
        tok = WindowToken(
            app_id=app_id,
            hwnd=win.hwnd,
            pid=getattr(win, "_pid", 0),
            state=win.state,
            label=label,
            created_at=time.time(),
        )
        _window_tokens[token_id] = tok
        app_tokens[idx] = token_id
        options.append({
            "token_id": token_id,
            "index": idx,
            "label": label,
            "state": win.state,
        })
    _pending_window_selections[app_id] = app_tokens
    _latest_pending_selection = {
        "app_id": app_id,
        "action": action,
        "tokens": app_tokens,
        "options": options,
        "created_at": time.time(),
    }
    return options


def get_pending_window_token(app_id: str, index: int) -> str | None:
    """Retrieve token_id for an index from pending selections for this app."""
    return _pending_window_selections.get(app_id, {}).get(index)


ORDINAL_OR_WORD_TO_INDEX: dict[str, int] = {
    "1": 1, "one": 1, "1st": 1, "first": 1, "main": 1,
    "2": 2, "two": 2, "2nd": 2, "second": 2, "secondary": 2,
    "3": 3, "three": 3, "3rd": 3, "third": 3,
    "4": 4, "four": 4, "4th": 4, "fourth": 4,
    "5": 5, "five": 5, "5th": 5, "fifth": 5,
}


def parse_window_choice(choice: str | int) -> int | None:
    """Parse user choice (spoken or typed) to an integer index (1-based).

    Supports:
    - 1, 2, 3
    - "1", "window 1", "win 1", "window one"
    - "main", "main one", "the main one", "main window", "the main window" -> 1
    - "secondary", "secondary one", "the secondary one", "secondary window" -> 2
    - "first", "first one", "the first one", "1st", "1st one" -> 1
    - "second", "second one", "the second one", "2nd", "2nd one" -> 2
    - "choose 1", "pick the second one", "select window 2"
    """
    if isinstance(choice, int):
        return choice if choice > 0 else None

    text = str(choice).strip().lower()
    # Strip conversational prefixes: "hey orion", "please", "can you", "could you", "choose", "pick", "select"
    text = re.sub(
        r"^(?:(?:hey\s+|hi\s+)?orion[,\s]+)?(?:please\s+)?(?:can\s+you\s+|could\s+you\s+)?",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    text = re.sub(
        r"^(?:choose|pick|select|switch\s+to|focus|maximize|minimise|minimize|restore|bring\s+up|take)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    text = re.sub(r"^the\s+", "", text, flags=re.IGNORECASE).strip()
    text = text.strip(" .!?\"'")

    # Pattern: "window 1", "win 1", "window main", "window second", "window one"
    m_win = re.match(r"^(?:window|win)\s+(.+?)(?:\s+one|\s+window)?$", text, flags=re.IGNORECASE)
    if m_win:
        cand = m_win.group(1).strip()
        if cand in ORDINAL_OR_WORD_TO_INDEX:
            return ORDINAL_OR_WORD_TO_INDEX[cand]
        if cand.isdigit():
            val = int(cand)
            return val if val > 0 else None

    # Pattern: "<token> one" or "<token> window" (e.g. "main one", "second one", "first one", "secondary window")
    m_one = re.match(r"^(.+?)\s+(?:one|window)$", text, flags=re.IGNORECASE)
    if m_one:
        cand = m_one.group(1).strip()
        if cand in ORDINAL_OR_WORD_TO_INDEX:
            return ORDINAL_OR_WORD_TO_INDEX[cand]
        if cand.isdigit():
            val = int(cand)
            return val if val > 0 else None

    # Direct match in dictionary
    if text in ORDINAL_OR_WORD_TO_INDEX:
        return ORDINAL_OR_WORD_TO_INDEX[text]

    if text.isdigit():
        val = int(text)
        return val if val > 0 else None

    return None


def clear_pending_window_selection() -> None:
    """Clear active pending window selection state."""
    global _latest_pending_selection
    _latest_pending_selection = None


def is_pending_window_selection_active() -> bool:
    """Return True if there is an active, unexpired pending window selection."""
    global _latest_pending_selection
    if not _latest_pending_selection:
        return False
    if time.time() - _latest_pending_selection.get("created_at", 0) > 300.0:
        _latest_pending_selection = None
        return False
    return True


def get_latest_pending_selection() -> dict[str, Any] | None:
    """Return latest pending window selection dict if still valid."""
    if is_pending_window_selection_active():
        return _latest_pending_selection
    return None


def resolve_choice_to_pending(choice: str | int) -> tuple[str, str, str, str] | None:
    """Resolve user choice (e.g. 1, '1', 'window 1', 'main one', 'second one') to (app_id, action, token_id, label)."""
    global _latest_pending_selection
    if not _latest_pending_selection:
        return None
    if time.time() - _latest_pending_selection.get("created_at", 0) > 300.0:
        _latest_pending_selection = None
        return None

    idx = parse_window_choice(choice)
    if idx is None:
        return None

    tokens = _latest_pending_selection.get("tokens", {})
    token_id = tokens.get(idx)
    if not token_id:
        return None

    tok = _window_tokens.get(token_id)
    label = tok.label if tok else f"Window {idx}"
    return (
        _latest_pending_selection["app_id"],
        _latest_pending_selection.get("action", "focus"),
        token_id,
        label,
    )


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


def _is_target_window_foreground(fg_hwnd: int, target_hwnd: int, app_id: str) -> bool:
    """Verify strictly whether the actual foreground window matches the target window.

    Accounts for:
    - Direct handle match (fg_hwnd == target_hwnd)
    - Root ancestor match via GA_ROOT (e.g. child control or CoreWindow in ApplicationFrameHost)
    - Direct child relationship via IsChild
    - Reverse child relationship (in case target_hwnd is a child and fg_hwnd is top-level frame)
    - Strict rejection if foreground belongs to any other window or application
    """
    if not fg_hwnd or not target_hwnd:
        return False

    if fg_hwnd == target_hwnd:
        return True

    if sys.platform != "win32":
        return False

    import ctypes
    user32 = ctypes.windll.user32
    GA_ROOT = 2

    try:
        # Check root ancestor match (e.g. child CoreWindow inside top-level frame)
        if hasattr(user32, "GetAncestor"):
            fg_root = user32.GetAncestor(fg_hwnd, GA_ROOT)
            target_root = user32.GetAncestor(target_hwnd, GA_ROOT)

            if isinstance(fg_root, int) and not isinstance(fg_root, bool) and fg_root != 0:
                if fg_root == target_hwnd:
                    return True
                if isinstance(target_root, int) and not isinstance(target_root, bool) and target_root != 0:
                    if fg_root == target_root:
                        return True

            if isinstance(target_root, int) and not isinstance(target_root, bool) and target_root != 0:
                if target_root == fg_hwnd:
                    return True

        # Check child relationship via IsChild
        if hasattr(user32, "IsChild"):
            is_child_res = user32.IsChild(target_hwnd, fg_hwnd)
            if isinstance(is_child_res, (int, bool)) and is_child_res not in (0, False):
                return True
            is_child_rev = user32.IsChild(fg_hwnd, target_hwnd)
            if isinstance(is_child_rev, (int, bool)) and is_child_rev not in (0, False):
                return True
    except Exception as err:
        logger.debug("Error checking window relationship: %s", err)

    return False


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
        kernel32 = ctypes.windll.kernel32 if hasattr(ctypes.windll, "kernel32") else None
        cur_thread = kernel32.GetCurrentThreadId() if kernel32 and hasattr(kernel32, "GetCurrentThreadId") else 0
        fg_hwnd = user32.GetForegroundWindow() if hasattr(user32, "GetForegroundWindow") else 0
        fg_thread = (
            user32.GetWindowThreadProcessId(fg_hwnd, None)
            if (fg_hwnd and hasattr(user32, "GetWindowThreadProcessId"))
            else 0
        )
        target_thread = (
            user32.GetWindowThreadProcessId(hwnd, None)
            if hasattr(user32, "GetWindowThreadProcessId")
            else 0
        )

        attached_fg = False
        attached_target = False

        try:
            # Grant permission to set foreground
            if hasattr(user32, "AllowSetForegroundWindow"):
                try:
                    user32.AllowSetForegroundWindow(-1)
                except Exception:
                    pass

            # Attach thread input if caller and target/foreground threads differ
            if hasattr(user32, "AttachThreadInput") and cur_thread:
                if fg_thread and fg_thread != cur_thread:
                    try:
                        attached_fg = bool(user32.AttachThreadInput(cur_thread, fg_thread, True))
                    except Exception:
                        pass
                if target_thread and target_thread != cur_thread and target_thread != fg_thread:
                    try:
                        attached_target = bool(user32.AttachThreadInput(cur_thread, target_thread, True))
                    except Exception:
                        pass

            # Handle minimized windows by restoring them first so they can receive focus
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, _SW_RESTORE)
                time.sleep(0.05)

            # Bring window to top and set foreground
            if hasattr(user32, "BringWindowToTop"):
                user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)

        finally:
            # Clean up thread input attachment immediately in a finally block
            if hasattr(user32, "AttachThreadInput") and cur_thread:
                if attached_fg and fg_thread:
                    try:
                        user32.AttachThreadInput(cur_thread, fg_thread, False)
                    except Exception:
                        pass
                if attached_target and target_thread:
                    try:
                        user32.AttachThreadInput(cur_thread, target_thread, False)
                    except Exception:
                        pass

        # Bounded verification window (up to ~300ms) for delayed foreground transitions
        focused = False
        for _ in range(6):
            fg = _get_foreground_hwnd_native()
            if fg and _is_target_window_foreground(fg, hwnd, app_id):
                focused = True
                break
            time.sleep(0.05)

        if focused:
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
                "error": f"{disp} was not brought to the foreground. Windows may have blocked the focus change or kept it behind another window.",
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
        # Handle minimized windows by restoring them first before maximizing
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, _SW_RESTORE)
            time.sleep(0.05)

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
    window_index: int | None = None,
) -> dict[str, Any]:
    """Shared implementation for all 4 window management tools.

    Enforces:
    - app_id is in the approved allowlist
    - Platform is Windows
    - Window re-enumeration and identity re-validation at execution time
    - Token expiry and stale-window detection
    - Direct targeting of active foreground window or specified window index when available
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

    # Check if window_token was given as a numeric string or 'window N'
    if window_token:
        import re
        m_idx = re.match(r"^(?:window\s+)?(\d+)$", str(window_token).strip(), re.IGNORECASE)
        if m_idx:
            window_index = int(m_idx.group(1))
            window_token = None

    # Token-based path: resolve token to a specific window
    if window_token:
        win, err = resolve_window_token(window_token, resolved_id)
        if err:
            return {"success": False, "error": err}
        if win is None:
            return {"success": False, "error": "The target window is no longer available."}
        return _verify_and_act_on_window(win.hwnd, resolved_id, action)

    # Window index path: 1-based index
    if window_index is not None:
        try:
            all_windows = _enumerate_windows_native()
        except Exception as err:
            logger.error("Failed to enumerate windows for %s: %s", resolved_id, err)
            return {"success": False, "error": "Desktop inspection temporarily unavailable."}
        app_windows = [w for w in all_windows if w.app_id == resolved_id]
        if not app_windows:
            return {"success": False, "error": f"{disp} does not have any open windows."}
        if 1 <= window_index <= len(app_windows):
            target_win = app_windows[window_index - 1]
            return _verify_and_act_on_window(target_win.hwnd, resolved_id, action)
        return {
            "success": False,
            "error": f"Invalid window index {window_index}. {disp} has {len(app_windows)} open window(s).",
        }

    # No token or index: enumerate and find target window(s)
    single_win, app_windows, enum_err = _get_single_app_window(resolved_id)

    if enum_err:
        return {"success": False, "error": enum_err}

    if not app_windows:
        return {"success": False, "error": f"{disp} does not have any open windows."}

    # Exactly one window -> act immediately
    if len(app_windows) == 1:
        return _verify_and_act_on_window(single_win.hwnd, resolved_id, action)

    # Multiple windows: check if one window is already the active foreground window
    active_wins = [w for w in app_windows if w.is_active]
    if len(active_wins) == 1:
        return _verify_and_act_on_window(active_wins[0].hwnd, resolved_id, action)

    # Ambiguous multiple windows -> prompt user with clean choices
    options = create_window_tokens(resolved_id, app_windows, action=action)
    return {
        "success": False,
        "requires_selection": True,
        "app_id": resolved_id,
        "display_name": disp,
        "windows": options,
    }


# =====================================================================
# PUBLIC WINDOW MANAGEMENT TOOLS (Step 11)
# =====================================================================

def focus_application_window(
    app_id: str,
    window_token: str | None = None,
    window_index: int | None = None,
) -> dict[str, Any]:
    """Bring an approved application window to the foreground.

    Requires confirmed re-enumeration before acting. Reports accurate success/failure.
    Never operates on unapproved applications or leaks process details.
    """
    return _window_management_core(app_id, window_token, "focus", window_index=window_index)


def minimize_application_window(
    app_id: str,
    window_token: str | None = None,
    window_index: int | None = None,
) -> dict[str, Any]:
    """Minimize an approved application window. Verifies state afterward."""
    return _window_management_core(app_id, window_token, "minimize", window_index=window_index)


def maximize_application_window(
    app_id: str,
    window_token: str | None = None,
    window_index: int | None = None,
) -> dict[str, Any]:
    """Maximize an approved application window. Verifies state afterward."""
    return _window_management_core(app_id, window_token, "maximize", window_index=window_index)


def restore_application_window(
    app_id: str,
    window_token: str | None = None,
    window_index: int | None = None,
) -> dict[str, Any]:
    """Restore a minimized/maximized approved application window to Normal state.

    Verifies state afterward. Never claims success if the final state does not confirm.
    """
    return _window_management_core(app_id, window_token, "restore", window_index=window_index)
