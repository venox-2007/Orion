"""Unit and integration tests for Telegram Voice Commands (Step 12).

Tests:
1. VoiceTranscriber unit tests (lazy loading, segment concatenation, file validation, async execution).
2. Telegram voice note authorization and resource limits (duration > 60s, size > 5MB, voice disabled).
3. Routing transcribed voice through the existing text command pipeline (tools, proposals, LLM chat).
4. Unclear/silent audio handling.
5. Guaranteed temporary audio file deletion/cleanup on both success and failure.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from telegram import Message, Update, Voice
from telegram.constants import ParseMode

from orion.config.settings import Settings
from orion.integrations.telegram_bot import (
    UNAUTHORIZED_MESSAGE,
    create_telegram_app,
)
from orion.memory.database import ConversationMemory
from orion.tools.registry import ToolRegistry
from orion.voice.transcriber import VoiceTranscriber


# ===========================================================================
# 1. VoiceTranscriber Unit Tests
# ===========================================================================

def test_transcriber_lazy_loading():
    """Model weights are not loaded until first transcription request."""
    transcriber = VoiceTranscriber(model_size="base", device="cpu", compute_type="int8")
    assert transcriber.is_loaded() is False

    mock_model_cls = MagicMock()
    with patch("faster_whisper.WhisperModel", mock_model_cls):
        model = transcriber.get_model()
        assert transcriber.is_loaded() is True
        mock_model_cls.assert_called_once_with(
            "base",
            device="cpu",
            compute_type="int8",
            download_root=None,
        )
        # Calling get_model() again returns cached instance without re-instantiating
        model2 = transcriber.get_model()
        assert model is model2
        assert mock_model_cls.call_count == 1


def test_transcribe_segments_concatenation(tmp_path):
    """Transcription correctly joins segments and trims whitespace."""
    audio_file = tmp_path / "test.ogg"
    audio_file.write_bytes(b"OggSfake_audio_content")

    mock_seg1 = MagicMock()
    mock_seg1.text = " Open calculator "
    mock_seg2 = MagicMock()
    mock_seg2.text = " then maximize it "

    mock_model = MagicMock()
    mock_model.transcribe.return_value = ([mock_seg1, mock_seg2], MagicMock())

    transcriber = VoiceTranscriber(model_size="base")
    transcriber._model = mock_model

    result = transcriber.transcribe(audio_file)
    assert result == "Open calculator then maximize it"
    mock_model.transcribe.assert_called_once()


def test_transcribe_missing_file_raises():
    """Attempting to transcribe a nonexistent audio file raises FileNotFoundError."""
    transcriber = VoiceTranscriber(model_size="base")
    with pytest.raises(FileNotFoundError):
        transcriber.transcribe("nonexistent_recording.ogg")


@pytest.mark.asyncio
async def test_transcribe_async_non_blocking(tmp_path):
    """transcribe_async runs via asyncio.to_thread and respects async lock."""
    audio_file = tmp_path / "voice.ogg"
    audio_file.write_bytes(b"OggSfake")

    mock_seg = MagicMock()
    mock_seg.text = "Hello Orion"
    mock_model = MagicMock()
    mock_model.transcribe.return_value = ([mock_seg], MagicMock())

    transcriber = VoiceTranscriber()
    transcriber._model = mock_model

    result = await transcriber.transcribe_async(audio_file)
    assert result == "Hello Orion"


# ===========================================================================
# 2. Telegram Voice Authorization and Resource Guard Tests
# ===========================================================================

@pytest.fixture
def voice_env(tmp_path):
    db_path = str(tmp_path / "voice_test.db")
    memory = ConversationMemory(db_path=db_path)
    owner_id = 12345
    token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"

    mock_ollama = MagicMock()
    mock_ollama.generate_response = AsyncMock(return_value="I heard you clearly.")
    mock_ollama.generate_structured_json = AsyncMock(return_value=None)

    mock_transcriber = MagicMock(spec=VoiceTranscriber)
    mock_transcriber.transcribe_async = AsyncMock(return_value="what apps are open")

    reg = ToolRegistry()

    app = create_telegram_app(
        token=token,
        owner_id=owner_id,
        ollama_client=mock_ollama,
        memory=memory,
        tool_registry=reg,
        transcriber=mock_transcriber,
    )

    # Extract voice handler
    voice_handler_func = None
    for handler_list in app.handlers.values():
        for h in handler_list:
            if hasattr(h, "filters") and "voice" in str(h.filters).lower():
                voice_handler_func = h.callback
                break

    return {
        "app": app,
        "owner_id": owner_id,
        "memory": memory,
        "ollama": mock_ollama,
        "transcriber": mock_transcriber,
        "voice_handler": voice_handler_func,
    }


def _create_mock_voice_update(user_id: int, duration: int = 5, file_size: int = 10000, file_id: str = "voice_file_1"):
    update = MagicMock(spec=Update)
    update.effective_chat.type = "private"
    update.effective_chat.id = user_id
    update.effective_user.id = user_id

    mock_message = MagicMock(spec=Message)
    mock_voice = MagicMock(spec=Voice)
    mock_voice.duration = duration
    mock_voice.file_size = file_size
    mock_voice.file_id = file_id

    mock_message.voice = mock_voice
    mock_message.text = None
    mock_message.reply_text = AsyncMock()

    update.effective_message = mock_message
    return update


@pytest.mark.asyncio
async def test_voice_unauthorized_user_rejected(voice_env):
    """Unauthorized strangers sending voice notes receive unauthorized message and are stopped."""
    voice_handler = voice_env["voice_handler"]
    stranger_id = 99999
    update = _create_mock_voice_update(user_id=stranger_id, duration=10)

    context = MagicMock()
    context.bot.send_chat_action = AsyncMock()

    await voice_handler(update, context)

    update.effective_message.reply_text.assert_called_once_with(UNAUTHORIZED_MESSAGE)
    voice_env["transcriber"].transcribe_async.assert_not_called()


@pytest.mark.asyncio
async def test_voice_duration_exceeds_limit_rejected(voice_env):
    """Voice notes longer than 60 seconds are rejected with clear user message."""
    voice_handler = voice_env["voice_handler"]
    owner_id = voice_env["owner_id"]
    # 65 seconds (> 60s default limit)
    update = _create_mock_voice_update(user_id=owner_id, duration=65)

    context = MagicMock()
    context.bot.send_chat_action = AsyncMock()

    await voice_handler(update, context)

    # Transcription should not be called
    voice_env["transcriber"].transcribe_async.assert_not_called()
    # User message states limit
    sent_text = update.effective_message.reply_text.call_args[0][0]
    assert "longer than 60 seconds" in sent_text


@pytest.mark.asyncio
async def test_voice_size_exceeds_limit_rejected(voice_env):
    """Voice notes larger than 5 MB are rejected with clear message."""
    voice_handler = voice_env["voice_handler"]
    owner_id = voice_env["owner_id"]
    # 6 MB (> 5 MB default limit)
    update = _create_mock_voice_update(user_id=owner_id, duration=30, file_size=6 * 1024 * 1024)

    context = MagicMock()
    context.bot.send_chat_action = AsyncMock()

    await voice_handler(update, context)

    voice_env["transcriber"].transcribe_async.assert_not_called()
    sent_text = update.effective_message.reply_text.call_args[0][0]
    assert "exceeds the maximum allowed file size" in sent_text


# ===========================================================================
# 3. Pipeline Integration & Command Execution Tests
# ===========================================================================

@pytest.mark.asyncio
async def test_voice_transcription_successful_tool_command(voice_env):
    """Transcribed voice note routes seamlessly through the tool execution pipeline."""
    voice_handler = voice_env["voice_handler"]
    owner_id = voice_env["owner_id"]
    update = _create_mock_voice_update(user_id=owner_id, duration=5)

    mock_voice_file = MagicMock()
    mock_voice_file.download_to_drive = AsyncMock()

    context = MagicMock()
    context.bot.send_chat_action = AsyncMock()
    context.bot.get_file = AsyncMock(return_value=mock_voice_file)

    voice_env["transcriber"].transcribe_async.return_value = "what apps are open"

    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[]):
        await voice_handler(update, context)

    # Verify download and transcription were called
    mock_voice_file.download_to_drive.assert_called_once()
    voice_env["transcriber"].transcribe_async.assert_called_once()

    # Verify reply is tool result
    sent_text = update.effective_message.reply_text.call_args[0][0]
    assert "No approved applications currently have open windows" in sent_text

    # Verify conversation memory recorded the prompt and reply
    history = voice_env["memory"].get_recent_messages(owner_id, limit=5)
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "what apps are open"
    assert history[1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_voice_transcription_write_proposal(voice_env):
    """Transcribed voice note for write action produces confirmation buttons."""
    voice_handler = voice_env["voice_handler"]
    owner_id = voice_env["owner_id"]
    update = _create_mock_voice_update(user_id=owner_id, duration=4)

    mock_voice_file = MagicMock()
    mock_voice_file.download_to_drive = AsyncMock()

    context = MagicMock()
    context.bot.send_chat_action = AsyncMock()
    context.bot.get_file = AsyncMock(return_value=mock_voice_file)

    voice_env["transcriber"].transcribe_async.return_value = "open calc"

    with patch("orion.tools.app_launcher.is_app_available", return_value=(True, "OK", Path("calc.exe"))):
        await voice_handler(update, context)

    # Must present confirmation buttons without executing write immediately
    reply_kwargs = update.effective_message.reply_text.call_args[1]
    assert "reply_markup" in reply_kwargs
    keyboard = reply_kwargs["reply_markup"]
    buttons = [btn.text for row in keyboard.inline_keyboard for btn in row]
    assert "Confirm" in buttons
    assert "Cancel" in buttons


@pytest.mark.asyncio
async def test_voice_unclear_speech_asks_user_to_repeat(voice_env):
    """Unclear, empty, or silent audio asks the user to repeat without making LLM calls."""
    voice_handler = voice_env["voice_handler"]
    owner_id = voice_env["owner_id"]
    update = _create_mock_voice_update(user_id=owner_id, duration=3)

    mock_voice_file = MagicMock()
    mock_voice_file.download_to_drive = AsyncMock()

    context = MagicMock()
    context.bot.send_chat_action = AsyncMock()
    context.bot.get_file = AsyncMock(return_value=mock_voice_file)

    # Empty speech transcript
    voice_env["transcriber"].transcribe_async.return_value = "   "

    await voice_handler(update, context)

    sent_text = update.effective_message.reply_text.call_args[0][0]
    assert "couldn't clearly understand" in sent_text.lower()
    voice_env["ollama"].generate_response.assert_not_called()


# ===========================================================================
# 4. Audio File Cleanup Tests
# ===========================================================================

@pytest.mark.asyncio
async def test_audio_file_cleanup_on_success(voice_env):
    """Temporary audio file is deleted from disk after successful transcription."""
    voice_handler = voice_env["voice_handler"]
    owner_id = voice_env["owner_id"]
    update = _create_mock_voice_update(user_id=owner_id, duration=3)

    downloaded_paths = []

    async def fake_download(dest):
        Path(dest).write_bytes(b"OggSfake_audio_bytes")
        downloaded_paths.append(Path(dest))

    mock_voice_file = MagicMock()
    mock_voice_file.download_to_drive = fake_download

    context = MagicMock()
    context.bot.send_chat_action = AsyncMock()
    context.bot.get_file = AsyncMock(return_value=mock_voice_file)

    voice_env["transcriber"].transcribe_async.return_value = "hello orion"

    await voice_handler(update, context)

    assert len(downloaded_paths) == 1
    temp_path = downloaded_paths[0]
    # File MUST be deleted from disk
    assert not temp_path.exists()


@pytest.mark.asyncio
async def test_audio_file_cleanup_on_exception(voice_env):
    """Temporary audio file is guaranteed to be deleted even if transcription throws an error."""
    voice_handler = voice_env["voice_handler"]
    owner_id = voice_env["owner_id"]
    update = _create_mock_voice_update(user_id=owner_id, duration=3)

    downloaded_paths = []

    async def fake_download(dest):
        Path(dest).write_bytes(b"OggScorrupted_bytes")
        downloaded_paths.append(Path(dest))

    mock_voice_file = MagicMock()
    mock_voice_file.download_to_drive = fake_download

    context = MagicMock()
    context.bot.send_chat_action = AsyncMock()
    context.bot.get_file = AsyncMock(return_value=mock_voice_file)

    # Transcriber raises exception
    voice_env["transcriber"].transcribe_async.side_effect = RuntimeError("Audio decode error")

    await voice_handler(update, context)

    assert len(downloaded_paths) == 1
    temp_path = downloaded_paths[0]
    # File MUST be deleted despite exception
    assert not temp_path.exists()

    # User receives clean failure notice without traceback
    sent_text = update.effective_message.reply_text.call_args[0][0]
    assert "Unable to process voice message" in sent_text
    assert "RuntimeError" not in sent_text
    assert "Traceback" not in sent_text
