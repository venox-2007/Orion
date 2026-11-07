"""Unit tests for Step 18.1: Telegram Freeze & Double Ollama Routing Fix."""

from unittest.mock import AsyncMock, MagicMock
import pytest

from orion.integrations.telegram_bot import create_telegram_app
from orion.memory.database import ConversationMemory
from orion.tools.router import route_and_execute_tool, should_bypass_llm_tool_router


@pytest.mark.asyncio
async def test_conceptual_and_greetings_bypass_llm_tool_router():
    """'What is a GPU?' and 'Hello' bypass generate_structured_json."""
    mock_ollama = MagicMock()
    mock_ollama.generate_structured_json = AsyncMock()

    test_phrases = [
        "What is a GPU?",
        "what is a CPU?",
        "Explain how RAM works",
        "Hello",
        "hi",
        "hey there",
    ]

    for phrase in test_phrases:
        assert should_bypass_llm_tool_router(phrase) is True
        res = await route_and_execute_tool(phrase, ollama_client=mock_ollama)
        assert res is None

    # Verify generate_structured_json was never called for any of these phrases
    mock_ollama.generate_structured_json.assert_not_called()


@pytest.mark.asyncio
async def test_ollama_exception_releases_lock_and_sends_fallback(tmp_path):
    """An Ollama exception in process_user_text() releases user_lock and sends the fallback message."""
    db_file = str(tmp_path / "test_err.db")
    mem = ConversationMemory(db_path=db_file)
    owner_id = 9999
    token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"

    mock_ollama = MagicMock()
    mock_ollama.generate_response = AsyncMock(side_effect=RuntimeError("Simulated Ollama crash"))

    app = create_telegram_app(token=token, owner_id=owner_id, ollama_client=mock_ollama, memory=mem)
    message_handler_func = app.handlers[0][-1].callback

    context = MagicMock()
    context.bot.send_chat_action = AsyncMock()

    update_owner = MagicMock()
    update_owner.effective_chat.type = "private"
    update_owner.effective_chat.id = owner_id
    update_owner.effective_user.id = owner_id
    update_owner.effective_message.reply_text = AsyncMock()
    update_owner.effective_message.text = "Hello"

    # Should handle error gracefully without raising
    await message_handler_func(update_owner, context)
    update_owner.effective_message.reply_text.assert_called_once()
    reply = update_owner.effective_message.reply_text.call_args[0][0]
    assert "Sorry, I took too long to process that request. Please try again." in reply


@pytest.mark.asyncio
async def test_subsequent_deterministic_query_succeeds_after_error(tmp_path):
    """A subsequent deterministic query ('How much RAM am I using?') succeeds immediately after."""
    db_file = str(tmp_path / "test_ram.db")
    mem = ConversationMemory(db_path=db_file)
    owner_id = 9999
    token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"

    mock_ollama = MagicMock()
    mock_ollama.generate_response = AsyncMock(side_effect=RuntimeError("Simulated Ollama crash"))

    app = create_telegram_app(token=token, owner_id=owner_id, ollama_client=mock_ollama, memory=mem)
    message_handler_func = app.handlers[0][-1].callback

    context = MagicMock()
    context.bot.send_chat_action = AsyncMock()

    # 1. Message that triggers exception
    update_err = MagicMock()
    update_err.effective_chat.type = "private"
    update_err.effective_chat.id = owner_id
    update_err.effective_user.id = owner_id
    update_err.effective_message.reply_text = AsyncMock()
    update_err.effective_message.text = "Hello"
    await message_handler_func(update_err, context)

    # 2. Subsequent deterministic RAM query succeeds immediately without being blocked
    update_ram = MagicMock()
    update_ram.effective_chat.type = "private"
    update_ram.effective_chat.id = owner_id
    update_ram.effective_user.id = owner_id
    update_ram.effective_message.reply_text = AsyncMock()
    update_ram.effective_message.text = "How much RAM am I using?"
    await message_handler_func(update_ram, context)

    update_ram.effective_message.reply_text.assert_called_once()
    reply = update_ram.effective_message.reply_text.call_args[0][0]
    assert "RAM usage:" in reply
