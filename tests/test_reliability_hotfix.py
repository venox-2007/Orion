"""Unit and integration tests for the Windows Control Reliability Hotfix.

Covers:
1. Sequential natural-language actions ("open calc then maximise it", "Open notepad and calculator, then minimize notepad", etc.)
2. Pronoun resolution ("it" -> previous app)
3. Window selection without unnecessary disambiguation (active window, window 1, clean prompts without tokens/PIDs)
4. Window focus reliability (restoring minimized, AttachThreadInput, clean cleanup, honest failure)
5. Natural Telegram responses without raw JSON, handles, or stack traces
6. Prerequisite dependency enforcement (skip dependent actions if launch fails)
"""

from pathlib import Path
from unittest.mock import MagicMock, call, patch
import pytest

from orion.tools.app_launcher import (
    execute_composite_app_action,
    get_approved_apps,
    resolve_app_alias,
)
from orion.tools.desktop_inspector import (
    WindowInfo,
    _verify_and_act_on_window,
    _window_management_core,
    focus_application_window,
    maximize_application_window,
    minimize_application_window,
    resolve_choice_to_pending,
    restore_application_window,
)
from orion.tools.router import (
    check_deterministic_app_launch,
    check_deterministic_multi_action,
    format_tool_result,
    parse_sequential_command,
)


def _make_win(hwnd: int, app_id: str, state: str = "Normal", is_active: bool = False) -> WindowInfo:
    approved = get_approved_apps()
    disp = approved[app_id].display_name if app_id in approved else app_id
    return WindowInfo(hwnd=hwnd, app_id=app_id, display_name=disp, state=state, is_active=is_active)


# ===========================================================================
# TASK 1 — SEQUENTIAL & MULTI-ACTION PARSING
# ===========================================================================

def test_parse_sequential_british_spelling():
    """'open calc then maximise it' parses accurately with British spelling."""
    assert parse_sequential_command("calculator then maximise it") == ("calculator", "maximize")
    assert parse_sequential_command("calc and minimise it") == ("calc", "minimize")

    with patch("orion.tools.app_launcher.is_app_available", return_value=(True, "Available", Path("C:/fake.exe"))):
        res = check_deterministic_multi_action("open calc then maximise it")
        assert res is not None
        assert res.get("is_proposal") is True
        args = res["arguments"]
        assert args["command_type"] == "launch_and_window"
        assert args["target_app_id"] == "calculator"
        assert args["window_action"] == "maximize"
        assert "maximize" in res["proposal_text"].lower()


def test_parse_multi_app_then_window_action():
    """'Open notepad and calculator, then minimize notepad' parses into 3 actions."""
    with patch("orion.tools.app_launcher.is_app_available", return_value=(True, "Available", Path("C:/fake.exe"))):
        res = check_deterministic_multi_action("Open notepad and calculator, then minimize notepad")
        assert res is not None
        assert res.get("is_proposal") is True
        args = res["arguments"]
        assert args["command_type"] == "composite"
        actions = args["actions"]
        assert len(actions) == 3
        assert actions[0] == {"action": "launch", "app_id": "notepad", "display_name": "Notepad"}
        assert actions[1] == {"action": "launch", "app_id": "calculator", "display_name": "Calculator"}
        assert actions[2] == {"action": "minimize", "app_id": "notepad", "display_name": "Notepad"}
        assert "Launch **Notepad**" in res["proposal_text"]
        assert "Launch **Calculator**" in res["proposal_text"]
        assert "Minimize **Notepad**" in res["proposal_text"]


def test_parse_open_and_bring_to_front_pronoun():
    """'Open VS Code and bring it to the front' resolves pronoun 'it' to VS Code."""
    with patch("orion.tools.app_launcher.is_app_available", return_value=(True, "Available", Path("C:/fake.exe"))):
        res = check_deterministic_multi_action("Open VS Code and bring it to the front")
        assert res is not None
        assert res.get("is_proposal") is True
        args = res["arguments"]
        assert args["command_type"] == "launch_and_window"
        assert args["target_app_id"] == "vscode"
        assert args["window_action"] == "focus"
        assert "VS Code" in res["proposal_text"]


def test_parse_window_action_then_open():
    """'Minimize calculator then open explorer' starts with window action, followed by launch."""
    with patch("orion.tools.app_launcher.is_app_available", return_value=(True, "Available", Path("C:/fake.exe"))):
        res = check_deterministic_multi_action("Minimize calculator then open explorer")
        assert res is not None
        assert res.get("is_proposal") is True
        args = res["arguments"]
        assert args["command_type"] == "composite"
        actions = args["actions"]
        assert len(actions) == 2
        assert actions[0]["action"] == "minimize"
        assert actions[0]["app_id"] == "calculator"
        assert actions[1]["action"] == "launch"
        assert actions[1]["app_id"] == "file_explorer"


def test_bounded_execution_limit():
    """Commands exceeding 4 actions are rejected cleanly for security and stability."""
    with patch("orion.tools.app_launcher.is_app_available", return_value=(True, "Available", Path("C:/fake.exe"))):
        # 5 actions: 4 launches + 1 minimize
        res = check_deterministic_multi_action("open calc, vs code, notepad and word, then minimize calc")
        assert res is not None
        assert res.get("is_proposal") is not True
        assert "at most 4" in res["reply"]


def test_dependent_action_skipped_on_prerequisite_failure():
    """If launch fails, dependent window actions on the same app are not executed."""
    # Calculator launch fails
    with patch("orion.tools.app_launcher.launch_application", return_value={"success": False, "error": "Crash"}), \
         patch("orion.tools.app_launcher.is_app_available", return_value=(True, "OK", Path("calc.exe"))), \
         patch("orion.tools.desktop_inspector.maximize_application_window") as mock_max:
        res = execute_composite_app_action(
            command_type="composite",
            actions=[
                {"action": "launch", "app_id": "calculator", "display_name": "Calculator"},
                {"action": "maximize", "app_id": "calculator", "display_name": "Calculator"},
            ],
        )
        assert res["success"] is False
        assert mock_max.called is False  # Dependent action was stopped!
        assert "Calculator" in res["failed"]
        assert "Calculator" in res["skipped"]
        assert res["message"] == "Couldn't launch Calculator."


# ===========================================================================
# TASK 2 — WINDOW SELECTION AVOIDING UNNECESSARY CLARIFICATION
# ===========================================================================

def test_single_window_acts_immediately():
    """When only 1 window is open, acts immediately without asking for clarification."""
    win = _make_win(0x1000, "calculator", "Normal")
    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[win]), \
         patch("sys.platform", "win32"), \
         patch("orion.tools.desktop_inspector._verify_and_act_on_window", return_value={"success": True, "message": "Done"}) as mock_act:
        res = _window_management_core("calculator", None, "maximize")
        assert res.get("requires_selection") is not True
        mock_act.assert_called_once_with(0x1000, "calculator", "maximize")


def test_active_foreground_window_acts_immediately_without_prompt():
    """When multiple windows exist but one is already active, targets active window immediately."""
    win1 = _make_win(0x1000, "calculator", "Normal", is_active=False)
    win2 = _make_win(0x2000, "calculator", "Normal", is_active=True)

    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[win1, win2]), \
         patch("sys.platform", "win32"), \
         patch("orion.tools.desktop_inspector._verify_and_act_on_window", return_value={"success": True, "message": "Maximized"}) as mock_act:
        res = _window_management_core("calculator", None, "maximize")
        assert res.get("requires_selection") is not True
        mock_act.assert_called_once_with(0x2000, "calculator", "maximize")


def test_command_with_explicit_window_index():
    """Targeting 'window 1' or 'window 2' acts on that specific window index."""
    win1 = _make_win(0x1000, "calculator", "Normal")
    win2 = _make_win(0x2000, "calculator", "Normal")

    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[win1, win2]), \
         patch("sys.platform", "win32"), \
         patch("orion.tools.desktop_inspector._verify_and_act_on_window", return_value={"success": True, "message": "Maximized"}) as mock_act:
        # Pass window_index=2
        res = _window_management_core("calculator", None, "maximize", window_index=2)
        assert res.get("requires_selection") is not True
        mock_act.assert_called_once_with(0x2000, "calculator", "maximize")

        # Pass "window 1" as window_token
        res2 = _window_management_core("calculator", "window 1", "maximize")
        assert res2.get("requires_selection") is not True
        mock_act.assert_called_with(0x1000, "calculator", "maximize")


def test_ambiguous_window_prompt_clean_format_no_tokens_or_handles():
    """When ambiguous, choices are formatted as clean numbered items without tokens or handles."""
    win1 = _make_win(0x1000, "calculator", "Normal")
    win2 = _make_win(0x2000, "calculator", "Minimized")

    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[win1, win2]), \
         patch("sys.platform", "win32"):
        res = _window_management_core("calculator", None, "maximize")
        assert res.get("requires_selection") is True
        reply = format_tool_result("maximize_application_window", res)
        # Required format check:
        # "I found two Calculator windows. Which one?
        # 1. Calculator — Main window
        # 2. Calculator — Secondary window"
        assert "I found two Calculator windows. Which one?" in reply
        assert "1. Calculator — Main window" in reply
        assert "2. Calculator — Secondary window" in reply
        # Strictly verify no technical tokens, handles, or PIDs are leaked
        assert "token:" not in reply.lower()
        assert "0x" not in reply
        assert "hwnd" not in reply.lower()
        assert "pid" not in reply.lower()


def test_user_text_response_resolves_pending_selection():
    """User replying '1' or 'window 1' resolves to the recorded pending window selection."""
    win1 = _make_win(0x1000, "calculator", "Normal")
    win2 = _make_win(0x2000, "calculator", "Normal")

    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[win1, win2]), \
         patch("sys.platform", "win32"):
        _window_management_core("calculator", None, "maximize")

        # User replies with "1"
        res1 = resolve_choice_to_pending("1")
        assert res1 is not None
        app_id, action, tok_id, label = res1
        assert app_id == "calculator"
        assert action == "maximize"
        assert "Main window" in label

        # User replies with "window 2"
        res2 = resolve_choice_to_pending("window 2")
        assert res2 is not None
        app_id2, action2, tok_id2, label2 = res2
        assert app_id2 == "calculator"
        assert "Secondary window" in label2


# ===========================================================================
# TASK 3 — FOREGROUND ACTIVATION RELIABILITY
# ===========================================================================

def test_focus_restores_minimized_window_and_attaches_threads():
    """focus restores iconic window, calls AttachThreadInput, and detaches in finally block."""
    hwnd = 0x5555
    mock_user32 = MagicMock()
    mock_user32.IsIconic.return_value = True
    mock_user32.GetForegroundWindow.return_value = 0x5555
    mock_user32.GetWindowThreadProcessId.return_value = 200

    mock_kernel32 = MagicMock()
    mock_kernel32.GetCurrentThreadId.return_value = 100

    with patch("sys.platform", "win32"), \
         patch("ctypes.windll") as mock_windll, \
         patch("orion.tools.desktop_inspector._is_target_window_foreground", return_value=True), \
         patch("time.sleep"):
        mock_windll.user32 = mock_user32
        mock_windll.kernel32 = mock_kernel32
        res = _verify_and_act_on_window(hwnd, "calculator", "focus")

    assert res["success"] is True
    # Verify ShowWindow(SW_RESTORE = 9) was called
    mock_user32.ShowWindow.assert_called_with(hwnd, 9)
    # Verify AllowSetForegroundWindow called
    mock_user32.AllowSetForegroundWindow.assert_called_with(-1)
    # Verify AttachThreadInput attached (True) then detached (False)
    mock_user32.AttachThreadInput.assert_has_calls([
        call(100, 200, True),
        call(100, 200, False),
    ], any_order=True)
    # Verify SetForegroundWindow called
    mock_user32.SetForegroundWindow.assert_called_with(hwnd)


def test_focus_reports_honest_failure_when_windows_blocks_focus():
    """Reports honest failure when Windows prevents foreground activation."""
    hwnd = 0x5555
    other_fg = 0x9999
    mock_user32 = MagicMock()
    mock_user32.IsIconic.return_value = False
    mock_user32.GetForegroundWindow.return_value = other_fg

    with patch("sys.platform", "win32"), \
         patch("ctypes.windll") as mock_windll, \
         patch("orion.tools.desktop_inspector._is_target_window_foreground", return_value=False), \
         patch("time.sleep"):
        mock_windll.user32 = mock_user32
        res = _verify_and_act_on_window(hwnd, "calculator", "focus")

    assert res["success"] is False
    assert "was not brought to the foreground" in res["error"]
    assert "Windows may have blocked the focus change" in res["error"] or "kept it behind another window" in res["error"]


# ===========================================================================
# TASK 4 — NATURAL TELEGRAM RESPONSES
# ===========================================================================

def test_natural_response_multi_launch_and_window():
    """'Opened Calculator and VS Code. Calculator is now maximized.'"""
    with patch("orion.tools.app_launcher.launch_application", return_value={"success": True}), \
         patch("orion.tools.app_launcher.is_app_available", return_value=(True, "OK", Path("fake.exe"))), \
         patch("orion.tools.desktop_inspector.maximize_application_window", return_value={"success": True}):
        res = execute_composite_app_action(
            command_type="composite",
            actions=[
                {"action": "launch", "app_id": "calculator", "display_name": "Calculator"},
                {"action": "launch", "app_id": "vscode", "display_name": "VS Code"},
                {"action": "maximize", "app_id": "calculator", "display_name": "Calculator"},
            ],
        )
        assert res["success"] is True
        assert res["message"] == "Opened Calculator and VS Code. Calculator is now maximized."


def test_natural_response_focus_blocked_by_windows():
    """'Opened Calculator, but Windows wouldn't bring it to the front.'"""
    with patch("orion.tools.app_launcher.launch_application", return_value={"success": True}), \
         patch("orion.tools.app_launcher.is_app_available", return_value=(True, "OK", Path("fake.exe"))), \
         patch("orion.tools.desktop_inspector.focus_application_window", return_value={"success": False, "error": "Windows kept it behind another window"}):
        res = execute_composite_app_action(
            command_type="composite",
            actions=[
                {"action": "launch", "app_id": "calculator", "display_name": "Calculator"},
                {"action": "focus", "app_id": "calculator", "display_name": "Calculator"},
            ],
        )
        assert res["success"] is True
        assert res["partial"] is True
        assert res["message"] == "Opened Calculator, but Windows wouldn't bring it to the front."


def test_natural_response_window_action_then_launch():
    """'Notepad minimized. File Explorer is now open.'"""
    with patch("orion.tools.desktop_inspector.minimize_application_window", return_value={"success": True}), \
         patch("orion.tools.app_launcher.launch_application", return_value={"success": True}), \
         patch("orion.tools.app_launcher.is_app_available", return_value=(True, "OK", Path("fake.exe"))):
        res = execute_composite_app_action(
            command_type="composite",
            actions=[
                {"action": "minimize", "app_id": "notepad", "display_name": "Notepad"},
                {"action": "launch", "app_id": "file_explorer", "display_name": "File Explorer"},
            ],
        )
        assert res["success"] is True
        assert res["message"] == "Notepad minimized. File Explorer is now open."
