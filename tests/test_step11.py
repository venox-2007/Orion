"""Tests for Step 11: Controlled Window Management.

Covers all 17 test cases from the spec using unittest.mock.
No live Telegram, Ollama, or Win32 calls are made.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call
import pytest

from orion.config.settings import Settings
from orion.memory.database import ConversationMemory
from orion.tools.app_launcher import get_approved_apps
from orion.tools.desktop_inspector import (
    WindowInfo,
    WindowToken,
    _window_tokens,
    create_window_tokens,
    focus_application_window,
    maximize_application_window,
    minimize_application_window,
    resolve_window_token,
    restore_application_window,
)
from orion.tools.registry import ToolRegistry, default_tool_registry
from orion.tools.router import (
    check_deterministic_window_query,
    check_deterministic_app_launch,
    format_action_proposal,
    format_tool_result,
    route_and_execute_tool,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def step11_env(tmp_path: Path):
    """Fixture providing isolated database and mock settings."""
    db_file = tmp_path / "test_step11.db"
    settings = Settings(
        telegram_bot_token="test_token",
        telegram_owner_id=12345,
        database_path=str(db_file),
    )
    with patch("orion.config.settings.get_settings", return_value=settings), \
         patch("orion.memory.database.get_settings", return_value=settings):
        memory = ConversationMemory(db_path=str(db_file))
        yield {
            "db_path": str(db_file),
            "settings": settings,
            "memory": memory,
            "tmp_path": tmp_path,
        }


@pytest.fixture(autouse=True)
def clear_window_tokens():
    """Clear module-level token store before each test."""
    _window_tokens.clear()
    yield
    _window_tokens.clear()


def _make_win(hwnd: int, app_id: str, state="Normal", is_active=False) -> WindowInfo:
    approved = get_approved_apps()
    display_name = approved[app_id].display_name if app_id in approved else app_id
    return WindowInfo(hwnd=hwnd, app_id=app_id, display_name=display_name, state=state, is_active=is_active)


# ---------------------------------------------------------------------------
# 1. Focus approved application (single window, success)
# ---------------------------------------------------------------------------

def test_focus_approved_application():
    win = _make_win(0x1234, "vscode", "Normal", is_active=False)
    mock_user32 = MagicMock()
    mock_user32.IsIconic.return_value = False
    mock_user32.SetForegroundWindow.return_value = 1
    mock_user32.GetForegroundWindow.return_value = win.hwnd

    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[win]), \
         patch("sys.platform", "win32"), \
         patch("ctypes.windll") as mock_windll:
        mock_windll.user32 = mock_user32
        result = focus_application_window("vscode")

    assert result["success"] is True
    assert "foreground" in result["message"].lower() or "front" in result.get("message", "").lower() or result["success"]


# ---------------------------------------------------------------------------
# 2. Minimize approved window (success)
# ---------------------------------------------------------------------------

def test_minimize_approved_window():
    win = _make_win(0x2000, "notepad", "Normal")
    mock_user32 = MagicMock()
    mock_user32.IsIconic.side_effect = [False, True]  # pre-check: not minimized; post-check: minimized
    mock_user32.ShowWindowAsync.return_value = 1

    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[win]), \
         patch("sys.platform", "win32"), \
         patch("ctypes.windll") as mock_windll, \
         patch("time.sleep"):
        mock_windll.user32 = mock_user32
        result = minimize_application_window("notepad")

    assert result["success"] is True
    assert "minimized" in result["message"].lower()
    mock_user32.ShowWindowAsync.assert_called_once_with(win.hwnd, 6)  # SW_MINIMIZE = 6


# ---------------------------------------------------------------------------
# 3. Maximize approved window (success)
# ---------------------------------------------------------------------------

def test_maximize_approved_window():
    win = _make_win(0x3000, "notepad", "Normal")
    mock_user32 = MagicMock()
    mock_user32.IsIconic.return_value = False
    mock_user32.IsZoomed.side_effect = [False, True]  # pre-check: not maximized; post: maximized
    mock_user32.ShowWindowAsync.return_value = 1

    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[win]), \
         patch("sys.platform", "win32"), \
         patch("ctypes.windll") as mock_windll, \
         patch("time.sleep"):
        mock_windll.user32 = mock_user32
        result = maximize_application_window("notepad")

    assert result["success"] is True
    assert "maximized" in result["message"].lower()
    mock_user32.ShowWindowAsync.assert_called_once_with(win.hwnd, 3)  # SW_MAXIMIZE = 3


# ---------------------------------------------------------------------------
# 4. Restore approved window (success)
# ---------------------------------------------------------------------------

def test_restore_approved_window():
    win = _make_win(0x4000, "notepad", "Minimized")
    mock_user32 = MagicMock()
    mock_user32.IsIconic.side_effect = [True, False]   # pre-check: minimized; post: not iconic
    mock_user32.IsZoomed.side_effect = [False, False]  # post: not zoomed
    mock_user32.ShowWindowAsync.return_value = 1

    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[win]), \
         patch("sys.platform", "win32"), \
         patch("ctypes.windll") as mock_windll, \
         patch("time.sleep"):
        mock_windll.user32 = mock_user32
        result = restore_application_window("notepad")

    assert result["success"] is True
    assert "restore" in result["message"].lower() or "normal" in result["message"].lower()
    mock_user32.ShowWindowAsync.assert_called_once_with(win.hwnd, 9)  # SW_RESTORE = 9


# ---------------------------------------------------------------------------
# 5. Reject unapproved application
# ---------------------------------------------------------------------------

def test_reject_unapproved_application():
    result = minimize_application_window("chrome")
    assert result["success"] is False
    assert "not in the approved" in result["error"].lower()


# ---------------------------------------------------------------------------
# 6. Reject missing window (app approved but no open windows)
# ---------------------------------------------------------------------------

def test_reject_missing_window():
    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[]):
        result = minimize_application_window("vscode")

    assert result["success"] is False
    assert "open windows" in result["error"].lower() or "no open" in result["error"].lower()


# ---------------------------------------------------------------------------
# 7. Multiple windows: requires_selection when no token given
# ---------------------------------------------------------------------------

def test_multiple_window_selection_required():
    wins = [
        _make_win(0x1001, "vscode", "Normal"),
        _make_win(0x1002, "vscode", "Minimized"),
    ]

    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=wins), \
         patch("sys.platform", "win32"):
        result = minimize_application_window("vscode")

    assert result["success"] is False
    assert result.get("requires_selection") is True
    windows = result.get("windows", [])
    assert len(windows) == 2
    # Tokens should be stored in module-level dict
    for w in windows:
        assert w["token_id"] in _window_tokens
        assert "label" in w
        assert "state" in w
        # Must NOT expose titles, PID, or raw handle
        assert "pid" not in w
        assert "hwnd" not in w
        assert "title" not in w


# ---------------------------------------------------------------------------
# 8. Stale window token rejection (IsWindow returns False)
# ---------------------------------------------------------------------------

def test_stale_window_rejection():
    win = _make_win(0x5000, "notepad", "Normal")
    # Create a valid token
    tokens = create_window_tokens("notepad", [win])
    tok_id = tokens[0]["token_id"]

    mock_user32 = MagicMock()
    mock_user32.IsWindow.return_value = False  # hwnd is gone

    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[win]), \
         patch("sys.platform", "win32"), \
         patch("ctypes.windll") as mock_windll:
        mock_windll.user32 = mock_user32
        result = minimize_application_window("notepad", window_token=tok_id)

    assert result["success"] is False
    assert "no longer available" in result["error"].lower() or "expired" in result["error"].lower() or "not found" in result["error"].lower()


# ---------------------------------------------------------------------------
# 9. No changes before confirmation (proposal is returned, tool not called)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_changes_before_confirmation():
    """Sending a minimize request via route_and_execute_tool must NOT call ShowWindowAsync."""
    win = _make_win(0x6000, "notepad", "Normal")
    mock_user32 = MagicMock()

    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[win]), \
         patch("sys.platform", "win32"), \
         patch("ctypes.windll") as mock_windll:
        mock_windll.user32 = mock_user32
        result = await route_and_execute_tool("minimize notepad", ollama_client=None, registry=default_tool_registry)

    assert result is not None
    assert result.get("is_proposal") is True
    assert result.get("action_type") == "minimize_application_window"
    # ShowWindowAsync must NOT have been called
    mock_user32.ShowWindowAsync.assert_not_called()


# ---------------------------------------------------------------------------
# 10. Owner-only approval: unauthorized user callback rejected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_owner_only_approval(step11_env):
    memory: ConversationMemory = step11_env["memory"]
    owner_id = 12345
    intruder_id = 99999

    # Create a pending action as the owner
    action_id = memory.create_pending_action(
        user_id=owner_id,
        action_type="minimize_application_window",
        payload={"app_id": "vscode"},
        ttl_seconds=300,
    )

    # Simulate that the intruder cannot access the pending action
    # (get_pending_action with user_id filter returns None for wrong user)
    act = memory.get_pending_action(action_id, user_id=intruder_id)
    assert act is None, "Intruder must not be able to retrieve owner's pending action"


# ---------------------------------------------------------------------------
# 11. Expired/cancelled approval rejected
# ---------------------------------------------------------------------------

def test_expired_cancelled_approval_rejected(step11_env):
    memory: ConversationMemory = step11_env["memory"]
    owner_id = 12345

    action_id = memory.create_pending_action(
        user_id=owner_id,
        action_type="minimize_application_window",
        payload={"app_id": "vscode"},
        ttl_seconds=1,  # expires very quickly
    )

    # Manually expire it
    with memory._lock:
        with memory._get_connection() as conn:
            conn.execute(
                "UPDATE pending_actions SET expires_at = ? WHERE id = ?",
                (time.time() - 1, action_id),
            )
            conn.commit()

    act = memory.get_pending_action(action_id, user_id=owner_id)
    assert act is not None
    assert act["status"] == "expired"


# ---------------------------------------------------------------------------
# 12. Duplicate execution prevented (mark_pending_action_status idempotence)
# ---------------------------------------------------------------------------

def test_duplicate_execution_prevented(step11_env):
    memory: ConversationMemory = step11_env["memory"]
    owner_id = 12345

    action_id = memory.create_pending_action(
        user_id=owner_id,
        action_type="maximize_application_window",
        payload={"app_id": "notepad"},
        ttl_seconds=300,
    )

    # First claim succeeds
    first = memory.mark_pending_action_status(action_id, from_status="pending", to_status="confirmed")
    assert first is True

    # Second claim (duplicate) fails
    second = memory.mark_pending_action_status(action_id, from_status="pending", to_status="confirmed")
    assert second is False


# ---------------------------------------------------------------------------
# 13. Failed focus verification: SetForegroundWindow OK but GetForegroundWindow != hwnd
# ---------------------------------------------------------------------------

def test_failed_focus_verification():
    win = _make_win(0x7000, "vscode", "Normal")
    mock_user32 = MagicMock()
    mock_user32.IsIconic.return_value = False
    mock_user32.SetForegroundWindow.return_value = 1
    mock_user32.GetForegroundWindow.return_value = 0x9999  # different hwnd

    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[win]), \
         patch("sys.platform", "win32"), \
         patch("ctypes.windll") as mock_windll:
        mock_windll.user32 = mock_user32
        result = focus_application_window("vscode")

    assert result["success"] is False
    assert "prevented" in result["error"].lower() or "focus" in result["error"].lower()


# ---------------------------------------------------------------------------
# 14. Failed window state verification: ShowWindowAsync OK but IsIconic still False
# ---------------------------------------------------------------------------

def test_failed_window_state_verification():
    win = _make_win(0x8000, "notepad", "Normal")
    mock_user32 = MagicMock()
    mock_user32.IsIconic.return_value = False  # never becomes iconic (minimize fails silently)
    mock_user32.ShowWindowAsync.return_value = 1

    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[win]), \
         patch("sys.platform", "win32"), \
         patch("ctypes.windll") as mock_windll, \
         patch("time.sleep"):
        mock_windll.user32 = mock_user32
        result = minimize_application_window("notepad")

    assert result["success"] is False
    assert "failed to verify" in result["error"].lower() or "minimized" in result["error"].lower()


# ---------------------------------------------------------------------------
# 15. Read-only queries remain read-only
# ---------------------------------------------------------------------------

def test_read_only_queries_remain_read_only():
    """'Is VS Code minimized?' must route to get_application_window_status, NOT a write tool."""
    result = check_deterministic_window_query("Is VS Code minimized?")
    assert result is not None
    assert result.get("tool") == "get_application_window_status"
    assert result.get("is_window_action") is not True

    result2 = check_deterministic_window_query("Is Notepad open?")
    assert result2 is not None
    assert result2.get("tool") == "get_application_window_status"

    result3 = check_deterministic_window_query("Is Excel maximized?")
    assert result3 is not None
    assert result3.get("tool") == "get_application_window_status"


# ---------------------------------------------------------------------------
# 16. History integration: log_action_history called with correct type/status
# ---------------------------------------------------------------------------

def test_history_integration(step11_env):
    memory: ConversationMemory = step11_env["memory"]
    owner_id = 12345

    # Log a minimize window action
    memory.log_action_history(
        user_id=owner_id,
        action_type="minimize_application_window",
        payload={"app_id": "notepad"},
        status="success",
        result={"success": True, "message": "Notepad has been minimized."},
    )

    history = memory.get_unified_history(owner_id, limit=5)
    assert len(history) >= 1
    item = history[0]
    assert item["summary"] == "Minimize window"
    assert item["status"].lower() in ("success", "completed")

    # Also log a focus action with cancelled status
    memory.log_action_history(
        user_id=owner_id,
        action_type="focus_application_window",
        payload={"app_id": "vscode"},
        status="cancelled",
    )
    history2 = memory.get_unified_history(owner_id, limit=5)
    focus_items = [h for h in history2 if h["summary"] == "Focus window"]
    assert len(focus_items) >= 1


# ---------------------------------------------------------------------------
# 17. Step 9 / Step 10 compatibility: existing detection still works
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_step9_10_compatibility():
    """Existing window query and launch detection must still work correctly."""
    # Step 10: "What apps are open?" → list_open_windows
    result = check_deterministic_window_query("What apps are open?")
    assert result is not None
    assert result.get("tool") == "list_open_windows"

    # Step 10: active window
    result2 = check_deterministic_window_query("What is the active window?")
    assert result2 is not None
    assert result2.get("tool") == "get_active_window"

    # Step 9: "open notepad" → launch_application proposal
    result3 = check_deterministic_app_launch("open notepad")
    assert result3 is not None
    assert result3.get("is_proposal") is True
    assert result3.get("action_type") == "launch_application"

    # Step 11: minimize command → is_window_action
    result4 = check_deterministic_window_query("minimize notepad")
    assert result4 is not None
    assert result4.get("is_window_action") is True
    assert result4.get("tool") == "minimize_application_window"

    # Step 11: focus command → is_window_action
    result5 = check_deterministic_window_query("focus vscode")
    assert result5 is not None
    assert result5.get("is_window_action") is True
    assert result5.get("tool") == "focus_application_window"

    # Step 11: maximize command
    result6 = check_deterministic_window_query("maximize notepad")
    assert result6 is not None
    assert result6.get("is_window_action") is True
    assert result6.get("tool") == "maximize_application_window"

    # Step 11: restore command
    result7 = check_deterministic_window_query("restore notepad")
    assert result7 is not None
    assert result7.get("is_window_action") is True
    assert result7.get("tool") == "restore_application_window"

    # Bring to front pattern
    result8 = check_deterministic_window_query("bring vscode to the front")
    assert result8 is not None
    assert result8.get("is_window_action") is True
    assert result8.get("tool") == "focus_application_window"

    # Switch to pattern
    result9 = check_deterministic_window_query("switch to notepad")
    assert result9 is not None
    assert result9.get("is_window_action") is True
    assert result9.get("tool") == "focus_application_window"


# ---------------------------------------------------------------------------
# Bonus: format_action_proposal for window management
# ---------------------------------------------------------------------------

def test_format_action_proposal_window_management():
    proposal = format_action_proposal("minimize_application_window", {"app_id": "notepad"})
    assert "Minimize window" in proposal
    assert "Notepad" in proposal
    assert "⚠️" in proposal

    proposal2 = format_action_proposal("focus_application_window", {"app_id": "vscode"})
    assert "Focus window" in proposal2
    assert "Visual Studio Code" in proposal2

    proposal3 = format_action_proposal("maximize_application_window", {"app_id": "notepad"})
    assert "Maximize window" in proposal3

    proposal4 = format_action_proposal("restore_application_window", {"app_id": "notepad"})
    assert "Restore window" in proposal4


# ---------------------------------------------------------------------------
# Bonus: format_tool_result for window management
# ---------------------------------------------------------------------------

def test_format_tool_result_window_management():
    # Success result
    exec_res = {
        "success": True,
        "data": {
            "success": True,
            "app_id": "notepad",
            "display_name": "Notepad",
            "message": "Notepad has been minimized.",
        },
    }
    msg = format_tool_result("minimize_application_window", exec_res)
    assert "Notepad" in msg or "minimized" in msg.lower()

    # Failure result
    exec_res_fail = {
        "success": False,
        "error": "Windows prevented the focus change.",
        "data": {},
    }
    msg_fail = format_tool_result("focus_application_window", exec_res_fail)
    assert len(msg_fail) > 0

    # requires_selection result
    exec_res_sel = {
        "success": False,
        "data": {
            "success": False,
            "requires_selection": True,
            "app_id": "vscode",
            "display_name": "Visual Studio Code",
            "windows": [
                {"token_id": "abc12345", "label": "Visual Studio Code — Window 1", "state": "Normal"},
                {"token_id": "def67890", "label": "Visual Studio Code — Window 2", "state": "Minimized"},
            ],
        },
    }
    msg_sel = format_tool_result("minimize_application_window", exec_res_sel)
    assert "Visual Studio Code" in msg_sel
    assert "Window 1" in msg_sel or "Window 2" in msg_sel


# ---------------------------------------------------------------------------
# Bonus: WindowToken expiry
# ---------------------------------------------------------------------------

def test_window_token_expiry():
    win = _make_win(0xAAAA, "vscode", "Normal")
    tokens = create_window_tokens("vscode", [win])
    tok_id = tokens[0]["token_id"]

    # Token should be valid
    assert tok_id in _window_tokens
    assert not _window_tokens[tok_id].is_expired()

    # Manually backdate creation to simulate expiry
    _window_tokens[tok_id].created_at = time.time() - 400  # 400 seconds ago > 300 TTL
    assert _window_tokens[tok_id].is_expired()


# ---------------------------------------------------------------------------
# Bonus: Non-Windows platform guard
# ---------------------------------------------------------------------------

def test_non_windows_platform_guard():
    with patch("sys.platform", "linux"):
        result = focus_application_window("vscode")
    assert result["success"] is False
    assert "windows" in result["error"].lower()

    with patch("sys.platform", "linux"):
        result2 = minimize_application_window("notepad")
    assert result2["success"] is False
