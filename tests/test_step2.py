"""Unit tests for Step 2: Telegram and local Ollama integration."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from orion.config.settings import Settings
from orion.integrations.ollama import (
    OLLAMA_UNAVAILABLE_MESSAGE,
    OllamaClient,
    clean_model_reply,
)
from orion.integrations.telegram_bot import (
    UNAUTHORIZED_MESSAGE,
    WELCOME_MESSAGE,
    create_telegram_app,
    is_authorized_owner,
    split_message,
)


def test_clean_model_reply() -> None:
    """Ensure thinking tags are stripped from responses."""
    raw = "<think>Analyzing user question...\nChecking facts...</think>Hello there!"
    cleaned = clean_model_reply(raw)
    assert cleaned == "Hello there!"

    multiline = "<think>\nThinking line 1\nThinking line 2\n</think>Answer"
    assert clean_model_reply(multiline) == "Answer"

    normal = "Just a direct response."
    assert clean_model_reply(normal) == "Just a direct response."


def test_split_message() -> None:
    """Ensure long messages are split within limit."""
    short = "Hello world"
    assert split_message(short, max_chunk=100) == ["Hello world"]

    long_text = ("First paragraph.\n" * 50) + ("Second paragraph.\n" * 50)
    chunks = split_message(long_text, max_chunk=200)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 200


def test_authorization_checks() -> None:
    """Ensure private chat and owner ID are strictly enforced."""
    owner_id = 12345

    # Valid owner in private chat
    update_valid = MagicMock()
    update_valid.effective_chat.type = "private"
    update_valid.effective_user.id = owner_id
    assert is_authorized_owner(update_valid, owner_id) is True

    # Unauthorized user
    update_unauth_user = MagicMock()
    update_unauth_user.effective_chat.type = "private"
    update_unauth_user.effective_user.id = 99999
    assert is_authorized_owner(update_unauth_user, owner_id) is False

    # Group chat (even if owner)
    update_group = MagicMock()
    update_group.effective_chat.type = "group"
    update_group.effective_user.id = owner_id
    assert is_authorized_owner(update_group, owner_id) is False


def test_settings_validation() -> None:
    """Verify settings validation requires token and positive owner ID."""
    s_invalid = Settings(telegram_bot_token=None, telegram_owner_id=None)
    with pytest.raises(ValueError) as exc:
        s_invalid.validate_telegram_configuration()
    assert "ORION_TELEGRAM_BOT_TOKEN" in str(exc.value)

    s_valid = Settings(telegram_bot_token="test_token", telegram_owner_id=12345)
    s_valid.validate_telegram_configuration()  # should not raise


@pytest.mark.asyncio
async def test_ollama_client_success() -> None:
    """Test successful Ollama response extraction with mocked httpx client."""
    client = OllamaClient(base_url="http://127.0.0.1:11434", model="qwen3.5:4b")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "message": {"content": "<think>private thought</think>Hello from ORION!"}
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        result = await client.generate_response("Hi")
        assert result == "Hello from ORION!"


@pytest.mark.asyncio
async def test_ollama_client_unavailable() -> None:
    """Test friendly message when Ollama is unavailable."""
    client = OllamaClient(base_url="http://127.0.0.1:11434")

    with patch("httpx.AsyncClient.post", side_effect=Exception("Connection refused")):
        result = await client.generate_response("Hi")
        assert result == OLLAMA_UNAVAILABLE_MESSAGE


@pytest.mark.asyncio
async def test_telegram_handlers() -> None:
    """Test Telegram start and message handlers with mocked updates."""
    mock_ollama = MagicMock()
    mock_ollama.generate_response = AsyncMock(return_value="Here is the answer.")

    owner_id = 55555
    token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"

    app = create_telegram_app(token=token, owner_id=owner_id, ollama_client=mock_ollama)

    # Extract start and text message handler callbacks
    start_handler_func = app.handlers[0][0].callback
    text_handler_func = app.handlers[0][1].callback

    context = MagicMock()
    context.bot.send_chat_action = AsyncMock()

    # 1. Start from authorized owner
    update_owner = MagicMock()
    update_owner.effective_chat.type = "private"
    update_owner.effective_chat.id = 55555
    update_owner.effective_user.id = 55555
    update_owner.effective_message.reply_text = AsyncMock()
    update_owner.effective_message.text = "/start"

    await start_handler_func(update_owner, context)
    update_owner.effective_message.reply_text.assert_called_with(WELCOME_MESSAGE)

    # 2. Start from unauthorized user
    update_stranger = MagicMock()
    update_stranger.effective_chat.type = "private"
    update_stranger.effective_chat.id = 99999
    update_stranger.effective_user.id = 99999
    update_stranger.effective_message.reply_text = AsyncMock()

    await start_handler_func(update_stranger, context)
    update_stranger.effective_message.reply_text.assert_called_with(UNAUTHORIZED_MESSAGE)

    # 3. Message from authorized owner
    update_owner.effective_message.reply_text.reset_mock()
    update_owner.effective_message.text = "Tell me a joke"

    await text_handler_func(update_owner, context)
    mock_ollama.generate_response.assert_called_with("Tell me a joke")
    update_owner.effective_message.reply_text.assert_called_with("Here is the answer.")

    # 4. Message from unauthorized user
    update_stranger.effective_message.reply_text.reset_mock()
    update_stranger.effective_message.text = "Hello stranger"

    await text_handler_func(update_stranger, context)
    update_stranger.effective_message.reply_text.assert_called_with(UNAUTHORIZED_MESSAGE)
