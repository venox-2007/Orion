"""Focused tests for Voice & Window Control Reliability Hotfix.

Covers:
a) Pending window selection and follow-up replies ("1", "window 1", "main one", "second one")
b) Voice/text selection equivalence
c) Expiration (> 5 mins) and cancellation
d) Sequential open -> maximize / bring to front commands
e) Authorization and allowlist preservation
"""

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import Update
from telegram.ext import Application

from orion.config.settings import Settings
from orion.integrations.telegram_bot import (
    UNAUTHORIZED_MESSAGE,
    create_telegram_app,
    interpret_voice_command,
)
from orion.memory.database import ConversationMemory
from orion.tools.app_launcher import get_approved_apps, is_app_blocked
from orion.tools.desktop_inspector import (
    WindowInfo,
    _latest_pending_selection,
    _verify_and_act_on_window,
    _window_management_core,
    _window_tokens,
    clear_pending_window_selection,
    create_window_tokens,
    is_pending_window_selection_active,
    parse_window_choice,
    resolve_choice_to_pending,
)
from orion.tools.registry import ToolRegistry, default_tool_registry
from orion.tools.router import (
    check_deterministic_app_launch,
    check_deterministic_multi_action,
    route_and_execute_tool,
)
from orion.voice.transcriber import VoiceTranscriber


def _make_win(hwnd: int, app_id: str, state: str = "Normal") -> WindowInfo:
    approved = get_approved_apps()
    disp = approved[app_id].display_name if app_id in approved else app_id
    return WindowInfo(
        hwnd=hwnd,
        app_id=app_id,
        display_name=disp,
        state=state,
        is_active=False,
    )


@pytest.fixture
def memory(tmp_path):
    db_path = tmp_path / "test_reliability.db"
    return ConversationMemory(db_path=str(db_path))


@pytest.fixture
def reliability_env(memory):
    owner_id = 12345
    token = "TEST_TOKEN_RELIABILITY"

    mock_ollama = MagicMock()
    mock_ollama.generate_response = AsyncMock(return_value="I heard you clearly.")
    mock_ollama.generate_structured_json = AsyncMock(return_value=None)

    mock_transcriber = MagicMock(spec=VoiceTranscriber)
    mock_transcriber.transcribe_async = AsyncMock(return_value="hello")

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
            transcriber=mock_transcriber,
        )

    # Extract callbacks
    voice_cb = None
    msg_cb = None
    cancel_cb = None
    callback_query_cb = None
    for h_list in app.handlers.values():
        for h in h_list:
            if hasattr(h, "filters") and "voice" in str(h.filters).lower():
                voice_cb = h.callback
            elif hasattr(h, "filters") and "text" in str(h.filters).lower():
                msg_cb = h.callback
            elif hasattr(h, "commands") and "cancel" in h.commands:
                cancel_cb = h.callback
            elif hasattr(h, "pattern"):
                callback_query_cb = h.callback

    return {
        "app": app,
        "owner_id": owner_id,
        "memory": memory,
        "ollama": mock_ollama,
        "transcriber": mock_transcriber,
        "voice_handler": voice_cb,
        "message_handler": msg_cb,
        "cancel_handler": cancel_cb,
        "callback_query_handler": callback_query_cb,
    }


# ===========================================================================
# A) PENDING WINDOW SELECTION & FOLLOW-UP REPLIES
# ===========================================================================

def test_parse_window_choice_natural_variations():
    """parse_window_choice correctly interprets natural typed or spoken expressions."""
    # Index 1 variations
    for phrase in ["1", "window 1", "win 1", "main", "main one", "the main one", "the main window",
                   "first", "first one", "the first one", "1st", "1st one", "window one", "choose 1", "pick the main one"]:
        assert parse_window_choice(phrase) == 1, f"Failed for {phrase}"

    # Index 2 variations
    for phrase in ["2", "window 2", "win 2", "second", "second one", "the second one", "secondary",
                   "secondary one", "the secondary one", "secondary window", "2nd", "2nd one",
                   "window two", "select window 2", "pick the second one"]:
        assert parse_window_choice(phrase) == 2, f"Failed for {phrase}"

    # Index 3 variations
    for phrase in ["3", "window 3", "third", "third one", "the third one", "3rd"]:
        assert parse_window_choice(phrase) == 3, f"Failed for {phrase}"

    # Negative / unrelated queries
    assert parse_window_choice("open calculator") is None
    assert parse_window_choice("delete file.txt") is None


def test_pending_window_selection_stores_action_and_resumes_on_reply():
    """When multiple windows exist, selecting 'main one' or 'second one' resumes the operation."""
    win1 = _make_win(0x1001, "calculator", "Normal")
    win2 = _make_win(0x1002, "calculator", "Normal")

    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[win1, win2]), \
         patch("sys.platform", "win32"):
        res = _window_management_core("calculator", None, "maximize")
        assert res.get("requires_selection") is True
        assert is_pending_window_selection_active() is True

        # Test resolving "main one" -> index 1
        sel1 = resolve_choice_to_pending("main one")
        assert sel1 is not None
        app_id, action, tok_id, label = sel1
        assert app_id == "calculator"
        assert action == "maximize"
        assert "Main window" in label

        # Test resolving "second one" -> index 2
        sel2 = resolve_choice_to_pending("second one")
        assert sel2 is not None
        app_id2, action2, tok_id2, label2 = sel2
        assert app_id2 == "calculator"
        assert action2 == "maximize"
        assert "Secondary window" in label2


@pytest.mark.asyncio
async def test_window_selection_revalidates_and_handles_closed_window(reliability_env):
    """If the target window closed before selection, ORION asks again safely without crashing."""
    msg_handler = reliability_env["message_handler"]
    owner_id = reliability_env["owner_id"]

    win1 = _make_win(0x1001, "calculator", "Normal")
    win2 = _make_win(0x1002, "calculator", "Normal")

    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[win1, win2]), \
         patch("sys.platform", "win32"):
        _window_management_core("calculator", None, "maximize")
        assert is_pending_window_selection_active() is True

    # Now simulate user replying "1", but win1 has closed (only win2 remains)
    update = MagicMock()
    update.effective_user.id = owner_id
    update.effective_chat.type = "private"
    update.effective_chat.id = owner_id
    update.effective_message.text = "main one"
    update.effective_message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot.send_chat_action = AsyncMock()

    # Re-enumeration returns only win2
    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[win2]), \
         patch("sys.platform", "win32"):
        await msg_handler(update, context)

    update.effective_message.reply_text.assert_called_once()
    reply_call = update.effective_message.reply_text.call_args[0][0]
    # Informs the user that the selected window is no longer available and asks safely
    assert "no longer available" in reply_call.lower()


# ===========================================================================
# B) VOICE / TEXT SELECTION EQUIVALENCE
# ===========================================================================

@pytest.mark.asyncio
async def test_voice_text_selection_equivalence(reliability_env):
    """Voice note with 'second one' and typed text 'second one' both resolve and resume operation."""
    msg_handler = reliability_env["message_handler"]
    voice_handler = reliability_env["voice_handler"]
    owner_id = reliability_env["owner_id"]
    memory = reliability_env["memory"]

    win1 = _make_win(0x2001, "calculator", "Normal")
    win2 = _make_win(0x2002, "calculator", "Normal")

    # 1. Test Typed Text path
    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[win1, win2]), \
         patch("sys.platform", "win32"):
        _window_management_core("calculator", None, "maximize")
        assert is_pending_window_selection_active() is True

    update_text = MagicMock()
    update_text.effective_user.id = owner_id
    update_text.effective_chat.type = "private"
    update_text.effective_chat.id = owner_id
    update_text.effective_message.text = "the second one"
    update_text.effective_message.reply_text = AsyncMock()

    ctx_text = MagicMock()
    ctx_text.bot.send_chat_action = AsyncMock()

    mock_user32 = MagicMock()
    mock_user32.IsWindow.return_value = True

    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[win1, win2]), \
         patch("orion.tools.desktop_inspector._verify_and_act_on_window", return_value={"success": True, "message": "Calculator has been maximized."}), \
         patch("ctypes.windll") as mock_windll, \
         patch("sys.platform", "win32"):
        mock_windll.user32 = mock_user32
        await msg_handler(update_text, ctx_text)

    update_text.effective_message.reply_text.assert_called_once()
    assert "maximized" in update_text.effective_message.reply_text.call_args[0][0].lower()
    # State cleared
    assert is_pending_window_selection_active() is False

    # 2. Test Voice Note path
    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[win1, win2]), \
         patch("sys.platform", "win32"):
        _window_management_core("calculator", None, "maximize")
        assert is_pending_window_selection_active() is True

    update_voice = MagicMock()
    update_voice.effective_user.id = owner_id
    update_voice.effective_chat.type = "private"
    update_voice.effective_chat.id = owner_id
    update_voice.effective_message.voice.duration = 2
    update_voice.effective_message.voice.file_size = 5000
    update_voice.effective_message.voice.file_id = "voice_file_equiv"
    update_voice.effective_message.reply_text = AsyncMock()

    mock_voice_file = MagicMock()
    async def fake_download(dest):
        Path(dest).write_bytes(b"fake_audio")
    mock_voice_file.download_to_drive = fake_download

    ctx = MagicMock()
    ctx.bot.get_file = AsyncMock(return_value=mock_voice_file)
    ctx.bot.send_chat_action = AsyncMock()

    # Whisper transcribes "the second one."
    reliability_env["transcriber"].transcribe_async.return_value = "the second one."

    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[win1, win2]), \
         patch("orion.tools.desktop_inspector._verify_and_act_on_window", return_value={"success": True, "message": "Calculator has been maximized."}), \
         patch("ctypes.windll") as mock_windll2, \
         patch("sys.platform", "win32"):
        mock_windll2.user32 = mock_user32
        await voice_handler(update_voice, ctx)

    update_voice.effective_message.reply_text.assert_called_once()
    assert "maximized" in update_voice.effective_message.reply_text.call_args[0][0].lower()
    assert is_pending_window_selection_active() is False


# ===========================================================================
# C) EXPIRATION AND CANCELLATION
# ===========================================================================

def test_pending_window_selection_expires_after_five_minutes():
    """Pending window selection expires after 300 seconds (5 minutes)."""
    win1 = _make_win(0x3001, "calculator", "Normal")
    win2 = _make_win(0x3002, "calculator", "Normal")

    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[win1, win2]), \
         patch("sys.platform", "win32"):
        _window_management_core("calculator", None, "minimize")
        assert is_pending_window_selection_active() is True

        # Simulate 301 seconds passing
        with patch("time.time", return_value=time.time() + 305):
            assert is_pending_window_selection_active() is False
            assert resolve_choice_to_pending("1") is None


@pytest.mark.asyncio
async def test_cancel_command_clears_pending_window_selection(reliability_env):
    """Sending /cancel clears active pending window selection state."""
    cancel_handler = reliability_env["cancel_handler"]
    owner_id = reliability_env["owner_id"]

    win1 = _make_win(0x3001, "calculator", "Normal")
    win2 = _make_win(0x3002, "calculator", "Normal")

    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[win1, win2]), \
         patch("sys.platform", "win32"):
        _window_management_core("calculator", None, "restore")
        assert is_pending_window_selection_active() is True

    update = MagicMock()
    update.effective_user.id = owner_id
    update.effective_chat.type = "private"
    update.effective_chat.id = owner_id
    update.effective_message.reply_text = AsyncMock()

    await cancel_handler(update, MagicMock())

    assert is_pending_window_selection_active() is False
    update.effective_message.reply_text.assert_called_once()
    assert "cancelled" in update.effective_message.reply_text.call_args[0][0].lower()


# ===========================================================================
# D) SEQUENTIAL OPEN -> MAXIMIZE COMMANDS
# ===========================================================================

def test_sequential_open_then_maximize_proposal():
    """'open calculator then maximize it' parses deterministically into composite action."""
    res = check_deterministic_multi_action("open calculator then maximize it")
    assert res is not None
    assert res.get("is_proposal") is True
    assert res.get("action_type") == "composite_app_action"
    args = res.get("arguments", {})
    assert args.get("command_type") == "launch_and_window"
    assert args.get("target_app_id") == "calculator"
    assert args.get("window_action") == "maximize"


def test_sequential_open_and_bring_to_front_proposal():
    """'open VS Code and bring it to the front' parses deterministically."""
    res = check_deterministic_multi_action("open VS Code and bring it to the front")
    assert res is not None
    assert res.get("is_proposal") is True
    assert res.get("action_type") == "composite_app_action"
    args = res.get("arguments", {})
    assert args.get("target_app_id") == "vscode"
    assert args.get("window_action") == "focus"


# ===========================================================================
# E) AUTHORIZATION AND ALLOWLIST PRESERVATION
# ===========================================================================

@pytest.mark.asyncio
async def test_unauthorized_user_voice_note_rejected(reliability_env):
    """Voice note from an unauthorized user is rejected immediately without processing."""
    voice_handler = reliability_env["voice_handler"]
    intruder_id = 99999

    update = MagicMock()
    update.effective_user.id = intruder_id
    update.effective_chat.type = "private"
    update.effective_chat.id = intruder_id
    update.effective_message.voice.duration = 3
    update.effective_message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot.send_chat_action = AsyncMock()

    await voice_handler(update, context)

    update.effective_message.reply_text.assert_called_once_with(UNAUTHORIZED_MESSAGE)
    # Transcriber must never be touched for unauthorized user
    reliability_env["transcriber"].transcribe_async.assert_not_called()


def test_unapproved_application_blocked_by_allowlist():
    """Non-allowlisted application commands are strictly rejected."""
    # Powershell is blocked
    blocked, reason = is_app_blocked("powershell")
    assert blocked is True
    assert "prohibited" in reason.lower()

    # Google Chrome is unapproved on this system
    blocked_chrome, reason_chrome = is_app_blocked("chrome")
    assert blocked_chrome is True


def test_ambiguous_destructive_voice_command_clarification():
    """Destructive or ambiguous voice commands ask for clarification rather than executing."""
    # "delete that"
    text, is_amb, clarif = interpret_voice_command("delete that")
    assert is_amb is True
    assert clarif is not None
    assert "clarify" in clarif.lower()

    # "remove it"
    text2, is_amb2, clarif2 = interpret_voice_command("remove it")
    assert is_amb2 is True
    assert "clarify" in clarif2.lower()
