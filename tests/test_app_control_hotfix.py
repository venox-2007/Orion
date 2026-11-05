"""Focused tests for Windows Application Control Hotfix.

Covers:
a. Multiple application names (e.g. 'open calc and vs code', 'launch notepad, calculator and explorer')
b. Application aliases (calc, calculator, vscode, vs code, visual studio code, explorer, file explorer, notepad)
c. Sequential commands (e.g. 'open calculator then maximize it')
d. Partial failures ('Opened Calculator, but couldn't launch VS Code')
e. Window focus verification (bring to front, minimized restore, blocked activation)
"""

from pathlib import Path
import time
from unittest.mock import MagicMock, patch
import pytest

from orion.tools.app_launcher import (
    APPROVED_APPLICATIONS,
    execute_composite_app_action,
    get_approved_apps,
    resolve_app_alias,
)
from orion.tools.desktop_inspector import (
    WindowInfo,
    focus_application_window,
    maximize_application_window,
)
from orion.tools.router import (
    check_deterministic_app_launch,
    format_action_proposal,
    format_tool_result,
    parse_multi_app_targets,
    parse_sequential_command,
)


# ---------------------------------------------------------------------------
# a. Multiple Application Names
# ---------------------------------------------------------------------------

def test_multiple_application_names_proposal():
    """Commands with multiple apps parse into composite proposals."""
    with patch("orion.tools.app_launcher.is_app_available", return_value=(True, "Available", Path("C:/fake.exe"))):
        # Two applications
        res = check_deterministic_app_launch("open calc and vs code")
        assert res is not None
        assert res.get("is_proposal") is True
        assert res["action_type"] == "composite_app_action"
        args = res["arguments"]
        assert args["command_type"] == "multi_launch"
        assert args["app_ids"] == ["calculator", "vscode"]
        assert "Calculator" in res["proposal_text"]
        assert "VS Code" in res["proposal_text"]

        # Three applications with commas and 'and'
        res2 = check_deterministic_app_launch("launch notepad, calculator and explorer")
        assert res2 is not None
        assert res2.get("is_proposal") is True
        args2 = res2["arguments"]
        assert args2["command_type"] == "multi_launch"
        assert args2["app_ids"] == ["notepad", "calculator", "file_explorer"]
        assert "Notepad" in res2["proposal_text"]
        assert "Calculator" in res2["proposal_text"]
        assert "File Explorer" in res2["proposal_text"]


def test_multiple_application_names_rejects_unapproved():
    """Multi-app commands reject blocked or unapproved apps safely."""
    # Blocked app in list
    res_blocked = check_deterministic_app_launch("open calc and chrome")
    assert res_blocked is not None
    assert res_blocked.get("is_proposal") is not True
    assert "not in the approved" in res_blocked["reply"].lower()

    # Unapproved unknown app in list
    res_unapproved = check_deterministic_app_launch("open calc and spotify")
    assert res_unapproved is not None
    assert res_unapproved.get("is_proposal") is not True
    assert "not in the approved" in res_unapproved["reply"].lower()


# ---------------------------------------------------------------------------
# b. Application Aliases
# ---------------------------------------------------------------------------

def test_application_aliases():
    """All required alias variants resolve deterministically."""
    # Calculator variants
    assert resolve_app_alias("calc") == "calculator"
    assert resolve_app_alias("calculator") == "calculator"
    assert resolve_app_alias("windows calculator") == "calculator"
    assert resolve_app_alias("the calc app") == "calculator"

    # VS Code variants
    assert resolve_app_alias("vscode") == "vscode"
    assert resolve_app_alias("vs code") == "vscode"
    assert resolve_app_alias("visual studio code") == "vscode"
    assert resolve_app_alias("code") == "vscode"
    assert resolve_app_alias("my vs code") == "vscode"

    # Explorer variants
    assert resolve_app_alias("explorer") == "file_explorer"
    assert resolve_app_alias("file explorer") == "file_explorer"
    assert resolve_app_alias("windows explorer") == "file_explorer"

    # Notepad variants
    assert resolve_app_alias("notepad") == "notepad"
    assert resolve_app_alias("windows notepad") == "notepad"
    assert resolve_app_alias("text editor") == "notepad"


# ---------------------------------------------------------------------------
# c. Sequential Commands
# ---------------------------------------------------------------------------

def test_sequential_commands_parsing():
    """Sequential launch + window action commands parse accurately."""
    # Helper parser tests
    assert parse_sequential_command("calculator then maximize it") == ("calculator", "maximize")
    assert parse_sequential_command("calc and maximize it") == ("calc", "maximize")
    assert parse_sequential_command("notepad then minimize it") == ("notepad", "minimize")
    assert parse_sequential_command("vscode and focus it") == ("vscode", "focus")
    assert parse_sequential_command("calc and vs code") is None  # Not sequential

    with patch("orion.tools.app_launcher.is_app_available", return_value=(True, "Available", Path("C:/fake.exe"))):
        # 'open calculator then maximize it'
        res = check_deterministic_app_launch("open calculator then maximize it")
        assert res is not None
        assert res.get("is_proposal") is True
        args = res["arguments"]
        assert args["command_type"] == "launch_and_window"
        assert args["target_app_id"] == "calculator"
        assert args["window_action"] == "maximize"
        assert "maximize" in res["proposal_text"].lower()
        assert "Calculator" in res["proposal_text"]

        # 'open notepad and minimize it'
        res2 = check_deterministic_app_launch("open notepad and minimize it")
        assert res2 is not None
        assert res2["arguments"]["command_type"] == "launch_and_window"
        assert res2["arguments"]["target_app_id"] == "notepad"
        assert res2["arguments"]["window_action"] == "minimize"


# ---------------------------------------------------------------------------
# d. Multi-Action Execution & Partial Failures
# ---------------------------------------------------------------------------

def test_multi_launch_all_succeed():
    """All requested applications launch successfully."""
    with patch("orion.tools.app_launcher.is_app_available", return_value=(True, "Available", Path("C:/fake.exe"))), \
         patch("orion.tools.app_launcher.launch_application", return_value={"success": True}):
        res = execute_composite_app_action("multi_launch", app_ids=["calculator", "vscode"])
        assert res["success"] is True
        assert res["message"] == "Opened Calculator and VS Code."


def test_multi_launch_three_succeed():
    """Three applications launched format natural comma list."""
    with patch("orion.tools.app_launcher.is_app_available", return_value=(True, "Available", Path("C:/fake.exe"))), \
         patch("orion.tools.app_launcher.launch_application", return_value={"success": True}):
        res = execute_composite_app_action("multi_launch", app_ids=["notepad", "calculator", "file_explorer"])
        assert res["success"] is True
        assert res["message"] == "Opened Notepad, Calculator and File Explorer."


def test_multi_launch_partial_failure():
    """Partial failure produces concise natural combined message."""
    def fake_launch(aid):
        if aid == "calculator":
            return {"success": True}
        return {"success": False, "error": "Launch failed"}

    with patch("orion.tools.app_launcher.is_app_available", return_value=(True, "Available", Path("C:/fake.exe"))), \
         patch("orion.tools.app_launcher.launch_application", side_effect=fake_launch):
        res = execute_composite_app_action("multi_launch", app_ids=["calculator", "vscode"])
        assert res["success"] is True
        assert res["message"] == "Opened Calculator, but couldn't launch VS Code."


def test_multi_launch_all_fail():
    """Total failure reports inability to launch requested apps."""
    with patch("orion.tools.app_launcher.is_app_available", return_value=(True, "Available", Path("C:/fake.exe"))), \
         patch("orion.tools.app_launcher.launch_application", return_value={"success": False, "error": "Error"}):
        res = execute_composite_app_action("multi_launch", app_ids=["calculator", "vscode"])
        assert res["success"] is False
        assert res["message"] == "Couldn't launch Calculator or VS Code."


def test_sequential_execution_success_and_partial():
    """Sequential launch-and-window action handles success and partial failure."""
    win = WindowInfo(hwnd=0x1234, app_id="calculator", display_name="Calculator", state="Normal")

    # Case 1: Both launch and maximize succeed
    with patch("orion.tools.app_launcher.is_app_available", return_value=(True, "Available", Path("C:/fake.exe"))), \
         patch("orion.tools.app_launcher.launch_application", return_value={"success": True}), \
         patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[win]), \
         patch("orion.tools.desktop_inspector.maximize_application_window", return_value={"success": True}), \
         patch("time.sleep"):
        res = execute_composite_app_action(
            "launch_and_window",
            target_app_id="calculator",
            window_action="maximize",
        )
        assert res["success"] is True
        assert res["message"] == "Opened and maximized Calculator."

    # Case 2: Launch succeeds, but maximize fails
    with patch("orion.tools.app_launcher.is_app_available", return_value=(True, "Available", Path("C:/fake.exe"))), \
         patch("orion.tools.app_launcher.launch_application", return_value={"success": True}), \
         patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[win]), \
         patch("orion.tools.desktop_inspector.maximize_application_window", return_value={"success": False, "error": "Window not found"}), \
         patch("time.sleep"):
        res2 = execute_composite_app_action(
            "launch_and_window",
            target_app_id="calculator",
            window_action="maximize",
        )
        assert res2["success"] is True
        assert res2["message"] == "Opened Calculator, but couldn't maximize it."


# ---------------------------------------------------------------------------
# e. Window Focus & Maximize Verification
# ---------------------------------------------------------------------------

def test_focus_restores_minimized_window():
    """Focusing a minimized window restores it before activating foreground."""
    win = WindowInfo(hwnd=0x5555, app_id="calculator", display_name="Calculator", state="Minimized")

    mock_user32 = MagicMock()
    mock_user32.IsIconic.return_value = True  # Starts minimized
    mock_user32.BringWindowToTop.return_value = 1
    mock_user32.SetForegroundWindow.return_value = 1
    mock_user32.GetForegroundWindow.return_value = 0x5555

    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[win]), \
         patch("sys.platform", "win32"), \
         patch("ctypes.windll") as mock_windll, \
         patch("time.sleep"):
        mock_windll.user32 = mock_user32
        result = focus_application_window("calculator")

    assert result["success"] is True
    # Verify SW_RESTORE (9) was called
    mock_user32.ShowWindow.assert_called_with(0x5555, 9)
    # Verify BringWindowToTop was called
    mock_user32.BringWindowToTop.assert_called_with(0x5555)


def test_maximize_restores_minimized_window():
    """Maximizing a minimized window restores it before calling ShowWindowAsync SW_MAXIMIZE."""
    win = WindowInfo(hwnd=0x6666, app_id="notepad", display_name="Notepad", state="Minimized")

    mock_user32 = MagicMock()
    mock_user32.IsIconic.side_effect = [True, False, False]
    mock_user32.IsZoomed.side_effect = [False, True, True]
    mock_user32.ShowWindowAsync.return_value = 1

    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[win]), \
         patch("sys.platform", "win32"), \
         patch("ctypes.windll") as mock_windll, \
         patch("time.sleep"):
        mock_windll.user32 = mock_user32
        result = maximize_application_window("notepad")

    assert result["success"] is True
    mock_user32.ShowWindow.assert_called_with(0x6666, 9)  # SW_RESTORE
    mock_user32.ShowWindowAsync.assert_called_with(0x6666, 3)  # SW_MAXIMIZE
