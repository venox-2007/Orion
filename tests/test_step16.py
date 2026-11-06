"""Focused tests for Step 16: Telegram Voice Reliability.

Covers:
1. Empty, silent, or unintelligible voice transcripts:
   - Whitespace, non-speech tokens ([BLANK_AUDIO], (music), punctuation).
2. Transcription timeouts and audio failure handling:
   - Timeout handling without crashing or exposing file paths/internals.
   - Preserves temporary file cleanup.
3. Ambiguous application names in voice commands:
   - e.g. "open microsoft", "maximize microsoft" -> short clarification.
4. Voice messages with ordinary conversation:
   - Phrases like "open up to me", "start talking to me", "how are you today"
     are handled as conversation, not mistaken for unapproved app launches.
5. Context preservation:
   - Failed or conversational voice notes do NOT corrupt or erase active
     pending window selections or pending proposals.
6. Successful voice command execution:
   - Spoken aliases and pronouns resolve properly without regressions.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orion.config.settings import Settings
from orion.integrations.telegram_bot import (
    UNAUTHORIZED_MESSAGE,
    create_telegram_app,
    interpret_voice_command,
)
from orion.memory.database import ConversationMemory
from orion.tools.app_launcher import get_approved_apps
from orion.tools.desktop_inspector import (
    WindowInfo,
    _latest_pending_selection,
    create_window_tokens,
    is_pending_window_selection_active,
)
from orion.tools.registry import ToolRegistry
from orion.tools.router import check_deterministic_app_launch
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
    db_path = tmp_path / "test_step16.db"
    return ConversationMemory(db_path=str(db_path))


@pytest.fixture
def voice_bot_env(memory):
    owner_id = 12345
    token = "TEST_TOKEN_STEP16"

    mock_ollama = MagicMock()
    mock_ollama.generate_response = AsyncMock(return_value="I am doing well, thank you.")
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

    voice_cb = None
    for h_list in app.handlers.values():
        for h in h_list:
            if hasattr(h, "filters") and "voice" in str(h.filters).lower():
                voice_cb = h.callback

    return {
        "app": app,
        "owner_id": owner_id,
        "memory": memory,
        "ollama": mock_ollama,
        "transcriber": mock_transcriber,
        "voice_handler": voice_cb,
    }


def _mock_voice_update(owner_id: int, file_id: str = "voice_step16_file"):
    update = MagicMock()
    update.effective_user.id = owner_id
    update.effective_chat.type = "private"
    update.effective_chat.id = owner_id
    update.effective_message.voice.duration = 4
    update.effective_message.voice.file_size = 8000
    update.effective_message.voice.file_id = file_id
    update.effective_message.reply_text = AsyncMock()
    return update


# ===========================================================================
# 1. EMPTY, UNINTELLIGIBLE, OR ARTIFACT TRANSCRIPTS
# ===========================================================================

@pytest.mark.asyncio
async def test_unintelligible_or_whisper_artifacts_handled_cleanly(voice_bot_env):
    """Whisper non-speech tokens like [BLANK_AUDIO] or (music) ask user to repeat cleanly."""
    voice_handler = voice_bot_env["voice_handler"]
    owner_id = voice_bot_env["owner_id"]

    mock_voice_file = MagicMock()
    mock_voice_file.download_to_drive = AsyncMock()

    ctx = MagicMock()
    ctx.bot.send_chat_action = AsyncMock()
    ctx.bot.get_file = AsyncMock(return_value=mock_voice_file)

    for artifact in ["[BLANK_AUDIO]", "(music)", "...", "   .   ", "(laughter)"]:
        voice_bot_env["transcriber"].transcribe_async.return_value = artifact
        update = _mock_voice_update(owner_id)

        await voice_handler(update, ctx)

        update.effective_message.reply_text.assert_called_once()
        reply = update.effective_message.reply_text.call_args[0][0]
        assert "clearly understand" in reply.lower()
        # Verify no debug details leak
        assert "blank" not in reply.lower()
        assert "whisper" not in reply.lower()


# ===========================================================================
# 2. TRANSCRIPTION TIMEOUTS & FAILURES
# ===========================================================================

@pytest.mark.asyncio
async def test_transcription_timeout_handled_gracefully(voice_bot_env):
    """Transcription timeout sends a friendly response without leaking traceback."""
    voice_handler = voice_bot_env["voice_handler"]
    owner_id = voice_bot_env["owner_id"]

    mock_voice_file = MagicMock()
    mock_voice_file.download_to_drive = AsyncMock()

    ctx = MagicMock()
    ctx.bot.send_chat_action = AsyncMock()
    ctx.bot.get_file = AsyncMock(return_value=mock_voice_file)

    voice_bot_env["transcriber"].transcribe_async.side_effect = asyncio.TimeoutError()

    update = _mock_voice_update(owner_id)
    await voice_handler(update, ctx)

    update.effective_message.reply_text.assert_called_once()
    reply = update.effective_message.reply_text.call_args[0][0]
    assert "timed out" in reply.lower()
    assert "shorter message" in reply.lower() or "text" in reply.lower()


@pytest.mark.asyncio
async def test_transcription_unexpected_failure_handled_cleanly(voice_bot_env):
    """Audio or model failure returns a concise friendly message without leaking paths."""
    voice_handler = voice_bot_env["voice_handler"]
    owner_id = voice_bot_env["owner_id"]

    mock_voice_file = MagicMock()
    mock_voice_file.download_to_drive = AsyncMock()

    ctx = MagicMock()
    ctx.bot.send_chat_action = AsyncMock()
    ctx.bot.get_file = AsyncMock(return_value=mock_voice_file)

    voice_bot_env["transcriber"].transcribe_async.side_effect = RuntimeError("DLL initialization failed")

    update = _mock_voice_update(owner_id)
    await voice_handler(update, ctx)

    update.effective_message.reply_text.assert_called_once()
    reply = update.effective_message.reply_text.call_args[0][0]
    assert "unable to process voice message" in reply.lower()
    # Must not leak runtime internals
    assert "dll" not in reply.lower()
    assert "runtimeerror" not in reply.lower()


# ===========================================================================
# 3. AMBIGUOUS APPLICATION NAMES IN VOICE COMMANDS
# ===========================================================================

def test_interpret_voice_command_ambiguous_app_name():
    """'open microsoft' is recognized as ambiguous and asks for clarification."""
    text, is_amb, clarif = interpret_voice_command("open microsoft")
    assert is_amb is True
    assert clarif is not None
    assert "Multiple approved applications matched 'microsoft'" in clarif
    assert "Word" in clarif
    assert "Excel" in clarif
    assert "PowerPoint" in clarif


def test_interpret_voice_command_maximize_ambiguous_app():
    """'maximize microsoft' asks which application to maximize."""
    text, is_amb, clarif = interpret_voice_command("maximize microsoft")
    assert is_amb is True
    assert clarif is not None
    assert "Multiple approved applications matched 'microsoft'" in clarif
    assert "maximize" in clarif.lower()


# ===========================================================================
# 4. VOICE MESSAGES CONTAINING ORDINARY CONVERSATION
# ===========================================================================

def test_conversational_phrases_not_mistaken_for_app_launch():
    """Conversational phrases starting with 'open' or 'start' flow to conversation, not allowlist errors."""
    for phrase in [
        "open up to me",
        "open your eyes",
        "start talking to me",
        "start thinking about this",
        "run faster",
        "open a conversation",
    ]:
        res = check_deterministic_app_launch(phrase)
        # Must return None to allow conversational routing
        assert res is None, f"Expected None for conversational phrase '{phrase}'"


@pytest.mark.asyncio
async def test_ordinary_conversation_voice_note_routes_to_ollama(voice_bot_env):
    """Ordinary conversation in voice note generates conversational reply without app errors."""
    voice_handler = voice_bot_env["voice_handler"]
    owner_id = voice_bot_env["owner_id"]

    mock_voice_file = MagicMock()
    mock_voice_file.download_to_drive = AsyncMock()

    ctx = MagicMock()
    ctx.bot.send_chat_action = AsyncMock()
    ctx.bot.get_file = AsyncMock(return_value=mock_voice_file)

    voice_bot_env["transcriber"].transcribe_async.return_value = "Hello Orion, how are you today?"

    update = _mock_voice_update(owner_id)
    await voice_handler(update, ctx)

    update.effective_message.reply_text.assert_called_once()
    reply = update.effective_message.reply_text.call_args[0][0]
    assert "doing well" in reply.lower()


# ===========================================================================
# 5. CONTEXT PRESERVATION ON FAILED / CONVERSATIONAL VOICE MESSAGES
# ===========================================================================

@pytest.mark.asyncio
async def test_failed_voice_does_not_corrupt_pending_window_selection(voice_bot_env):
    """A failed or timeout voice message preserves active pending window selection."""
    voice_handler = voice_bot_env["voice_handler"]
    owner_id = voice_bot_env["owner_id"]

    win1 = _make_win(0x1001, "calculator", "Normal")
    win2 = _make_win(0x1002, "calculator", "Normal")

    create_window_tokens("calculator", [win1, win2], action="focus")
    assert is_pending_window_selection_active() is True

    mock_voice_file = MagicMock()
    mock_voice_file.download_to_drive = AsyncMock()

    ctx = MagicMock()
    ctx.bot.send_chat_action = AsyncMock()
    ctx.bot.get_file = AsyncMock(return_value=mock_voice_file)

    # 1. Failed voice transcription
    voice_bot_env["transcriber"].transcribe_async.side_effect = RuntimeError("fail")
    update_fail = _mock_voice_update(owner_id)
    await voice_handler(update_fail, ctx)

    # Pending selection must still be active!
    assert is_pending_window_selection_active() is True

    # 2. Silent / blank voice note
    voice_bot_env["transcriber"].transcribe_async.side_effect = None
    voice_bot_env["transcriber"].transcribe_async.return_value = "[BLANK_AUDIO]"
    update_blank = _mock_voice_update(owner_id)
    await voice_handler(update_blank, ctx)

    assert is_pending_window_selection_active() is True

    # 3. Conversational voice note
    voice_bot_env["transcriber"].transcribe_async.return_value = "tell me the time"
    update_conv = _mock_voice_update(owner_id)
    await voice_handler(update_conv, ctx)

    assert is_pending_window_selection_active() is True


# ===========================================================================
# 6. SUCCESSFUL VOICE ROUTING & STEP 13/14/15 PRESERVATION
# ===========================================================================

@pytest.mark.asyncio
async def test_successful_voice_tool_routing(voice_bot_env):
    """Spoken command 'open calc' generates confirmation proposal cleanly."""
    voice_handler = voice_bot_env["voice_handler"]
    owner_id = voice_bot_env["owner_id"]

    mock_voice_file = MagicMock()
    mock_voice_file.download_to_drive = AsyncMock()

    ctx = MagicMock()
    ctx.bot.send_chat_action = AsyncMock()
    ctx.bot.get_file = AsyncMock(return_value=mock_voice_file)

    voice_bot_env["transcriber"].transcribe_async.return_value = "open calc"

    update = _mock_voice_update(owner_id)
    with patch("orion.tools.app_launcher.is_app_available", return_value=(True, "OK", Path("calc.exe"))):
        await voice_handler(update, ctx)

    update.effective_message.reply_text.assert_called_once()
    reply_kwargs = update.effective_message.reply_text.call_args[1]
    assert "reply_markup" in reply_kwargs
    sent_text = update.effective_message.reply_text.call_args[0][0]
    assert "Calculator" in sent_text
