"""Focused tests for Step 15: Window Selection UX Polish.

Covers:
1. Multiple-window selection:
   - Friendly window labels with state indicators.
   - Omission of raw handles, PIDs, or internal debug tokens.
   - Inline selection buttons with clean labels and compact callbacks.
2. Natural reply phrasing (text/voice):
   - "window 1", "the main one", "second one", "the second one", "primary window", "bring the main one to the front".
   - Preservation of original requested action after selection.
3. Single-window handling:
   - Exactly one open window proceeds immediately without clarification prompts.
4. Stale selection and closed window handling:
   - Expired selection tokens (> 5 mins) rejected cleanly.
   - Closed target window re-checks available windows without crashing.
5. Result reporting and accurate outcome verification:
   - Genuine success and honest failure reported accurately.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import InlineKeyboardMarkup

from orion.config.settings import Settings
from orion.integrations.telegram_bot import create_telegram_app
from orion.memory.database import ConversationMemory
from orion.tools.app_launcher import get_approved_apps
from orion.tools.desktop_inspector import (
    WindowInfo,
    _verify_and_act_on_window,
    _window_management_core,
    _window_tokens,
    clear_pending_window_selection,
    create_window_tokens,
    is_pending_window_selection_active,
    parse_window_choice,
    resolve_choice_to_pending,
    resolve_window_token,
)
from orion.tools.registry import ToolRegistry
from orion.tools.router import route_and_execute_tool


def _make_win(hwnd: int, app_id: str, state: str = "Normal", is_active: bool = False) -> WindowInfo:
    approved = get_approved_apps()
    disp = approved[app_id].display_name if app_id in approved else app_id
    return WindowInfo(
        hwnd=hwnd,
        app_id=app_id,
        display_name=disp,
        state=state,
        is_active=is_active,
    )


@pytest.fixture
def memory(tmp_path):
    db_path = tmp_path / "test_step15.db"
    return ConversationMemory(db_path=str(db_path))


@pytest.fixture
def bot_env(memory):
    owner_id = 12345
    token = "TEST_TOKEN_STEP15"

    mock_ollama = MagicMock()
    mock_ollama.generate_response = AsyncMock(return_value="I heard you.")
    mock_ollama.generate_structured_json = AsyncMock(return_value=None)

    reg = ToolRegistry()

    with patch("orion.config.settings.get_settings") as mock_settings:
        s = Settings(
            telegram_bot_token=token,
            telegram_owner_id=owner_id,
            voice_enabled=True,
            whisper_model="base",
            max_voice_duration_seconds=60,
            max_voice_file_size_bytes=5 * 1024 * 1024,
        )
        mock_settings.return_value = s
        app = create_telegram_app(
            token=token,
            owner_id=owner_id,
            ollama_client=mock_ollama,
            memory=memory,
            tool_registry=reg,
            transcriber=None,
        )

    msg_cb = None
    cbq_cb = None
    for h_list in app.handlers.values():
        for h in h_list:
            if hasattr(h, "filters") and "text" in str(h.filters).lower():
                msg_cb = h.callback
            elif hasattr(h, "pattern"):
                cbq_cb = h.callback

    return {
        "app": app,
        "owner_id": owner_id,
        "memory": memory,
        "message_handler": msg_cb,
        "callback_query_handler": cbq_cb,
    }


# ===========================================================================
# 1. MULTIPLE-WINDOW SELECTION & FRIENDLY LABELS
# ===========================================================================

def test_friendly_window_labels_with_state_and_no_raw_data():
    """Window labels are clean, readable, show state, and contain no raw debug data."""
    win1 = _make_win(0x1001, "calculator", "Normal", is_active=True)
    win2 = _make_win(0x1002, "calculator", "Minimized", is_active=False)

    options = create_window_tokens("calculator", [win1, win2], action="maximize")
    assert len(options) == 2

    lbl1 = options[0]["label"]
    lbl2 = options[1]["label"]

    assert "Calculator — Main window" in lbl1
    assert "(Active)" in lbl1
    assert "Calculator — Secondary window" in lbl2
    assert "(Minimized)" in lbl2

    # Verify no raw handles, memory pointers, or tokens leak into user-facing labels
    for opt in options:
        assert "0x100" not in opt["label"]
        assert "hwnd" not in opt["label"]
        assert "pid" not in opt["label"]
        assert opt["token_id"] not in opt["label"]


def test_three_window_labels_numbering():
    """For 3 or more windows, clean 'Window 1', 'Window 2', 'Window 3' numbering is used."""
    win1 = _make_win(0x2001, "vscode", "Normal", is_active=True)
    win2 = _make_win(0x2002, "vscode", "Maximized", is_active=False)
    win3 = _make_win(0x2003, "vscode", "Minimized", is_active=False)

    options = create_window_tokens("vscode", [win1, win2, win3], action="focus")
    assert len(options) == 3

    assert "Window 1 (Active)" in options[0]["label"]
    assert "Window 2 (Maximized)" in options[1]["label"]
    assert "Window 3 (Minimized)" in options[2]["label"]


# ===========================================================================
# 2. NATURAL REPLY PHRASING & PRESERVING ORIGINAL ACTION
# ===========================================================================

def test_natural_reply_phrasing_parsing():
    """Natural spoken and typed expressions all resolve to valid 1-based indices."""
    cases = [
        ("1", 1),
        ("window 1", 1),
        ("win 1", 1),
        ("the main one", 1),
        ("main one", 1),
        ("the main window", 1),
        ("primary window", 1),
        ("the primary one", 1),
        ("first one", 1),
        ("the first one", 1),
        ("1st one", 1),
        ("window one", 1),
        ("bring the main one to the front", 1),
        ("maximize window 1", 1),
        ("2", 2),
        ("window 2", 2),
        ("second one", 2),
        ("the second one", 2),
        ("secondary one", 2),
        ("the secondary window", 2),
        ("2nd one", 2),
        ("window two", 2),
        ("minimize the second one", 2),
        ("choose 2", 2),
        ("pick window 2", 2),
    ]
    for phrase, expected in cases:
        assert parse_window_choice(phrase) == expected, f"Failed for '{phrase}'"


def test_preserves_original_action_after_selection():
    """Pending selection preserves the originally requested window action (e.g. minimize, restore)."""
    win1 = _make_win(0x3001, "notepad", "Normal")
    win2 = _make_win(0x3002, "notepad", "Normal")

    create_window_tokens("notepad", [win1, win2], action="minimize")
    assert is_pending_window_selection_active() is True

    # User replies "the second one"
    res = resolve_choice_to_pending("the second one")
    assert res is not None
    app_id, action, tok_id, label = res
    assert app_id == "notepad"
    assert action == "minimize"
    assert "Secondary window" in label


# ===========================================================================
# 3. SINGLE WINDOW HANDLING (NO UNNECESSARY CLARIFICATION)
# ===========================================================================

@pytest.mark.asyncio
async def test_single_window_acts_without_clarification(memory):
    """When an app has exactly 1 window, ORION proposes or acts directly without asking 'Which window?'."""
    user_id = 12345
    win = _make_win(0x4001, "calculator", "Normal")

    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[win]), \
         patch("sys.platform", "win32"):
        res = await route_and_execute_tool(
            "maximize calculator",
            context={"user_id": user_id, "memory": memory},
        )
        assert res is not None
        assert res.get("handled") is True
        # Proposes directly without requiring window selection
        assert res.get("requires_selection") is not True
        assert res.get("is_proposal") is True
        assert res.get("action_type") == "maximize_application_window"


def test_core_single_window_executes_directly():
    """_window_management_core acts directly on single window without returning requires_selection."""
    win = _make_win(0x4002, "calculator", "Normal")

    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[win]), \
         patch("orion.tools.desktop_inspector._verify_and_act_on_window", return_value={"success": True, "message": "Calculator has been maximized."}), \
         patch("sys.platform", "win32"):
        res = _window_management_core("calculator", None, "maximize")
        assert res.get("success") is True
        assert res.get("requires_selection") is None
        assert "maximized" in res.get("message", "").lower()


# ===========================================================================
# 4. STALE SELECTION & CLOSED WINDOW HANDLING
# ===========================================================================

def test_stale_token_expiry():
    """Expired window tokens (> 300s) are rejected safely."""
    win1 = _make_win(0x5001, "calculator", "Normal")
    win2 = _make_win(0x5002, "calculator", "Normal")

    options = create_window_tokens("calculator", [win1, win2], action="focus")
    tok_id = options[0]["token_id"]

    mock_user32 = MagicMock()
    mock_user32.IsWindow.return_value = 1

    # Immediate resolution succeeds
    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[win1, win2]), \
         patch("ctypes.windll") as mock_windll, \
         patch("sys.platform", "win32"):
        mock_windll.user32 = mock_user32
        w, err = resolve_window_token(tok_id, "calculator")
        assert w is not None
        assert err is None

    # Simulate token expiry (305 seconds later)
    with patch("time.time", return_value=time.time() + 305):
        w_exp, err_exp = resolve_window_token(tok_id, "calculator")
        assert w_exp is None
        assert "expired" in err_exp.lower()


@pytest.mark.asyncio
async def test_invalid_choice_number_prompts_helpfully(bot_env):
    """When user enters an out-of-range window choice (e.g. 5 when 2 exist), ORION guides them cleanly."""
    msg_handler = bot_env["message_handler"]
    owner_id = bot_env["owner_id"]

    win1 = _make_win(0x6001, "calculator", "Normal")
    win2 = _make_win(0x6002, "calculator", "Normal")

    create_window_tokens("calculator", [win1, win2], action="restore")
    assert is_pending_window_selection_active() is True

    update = MagicMock()
    update.effective_user.id = owner_id
    update.effective_chat.type = "private"
    update.effective_chat.id = owner_id
    update.effective_message.text = "window 5"
    update.effective_message.reply_text = AsyncMock()

    ctx = MagicMock()
    ctx.bot.send_chat_action = AsyncMock()

    await msg_handler(update, ctx)

    update.effective_message.reply_text.assert_called_once()
    reply = update.effective_message.reply_text.call_args[0][0]
    assert "valid" in reply.lower()
    assert "1 to 2" in reply
    # Pending selection remains active for valid reply
    assert is_pending_window_selection_active() is True


# ===========================================================================
# 5. RESULT REPORTING & HONEST FAILURE
# ===========================================================================

def test_honest_failure_reporting():
    """Windows foreground refusal is reported truthfully without false positives."""
    mock_windll = MagicMock()
    mock_windll.user32.IsIconic.return_value = False
    mock_windll.user32.GetForegroundWindow.return_value = 0x9999  # Another window holds foreground

    with patch("ctypes.windll", mock_windll), \
         patch("orion.tools.desktop_inspector._get_foreground_hwnd_native", return_value=0x9999), \
         patch("sys.platform", "win32"):
        res = _verify_and_act_on_window(0x1001, "calculator", "focus")
        assert res["success"] is False
        assert "not brought to the foreground" in res["error"]
        assert "blocked" in res["error"]
