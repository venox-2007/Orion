"""Focused unit tests for Step 3: persistent conversation memory and formatting fix."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from telegram.constants import ParseMode

from orion.integrations.formatter import markdown_to_telegram_html
from orion.integrations.ollama import OllamaClient
from orion.integrations.telegram_bot import (
    RESET_CONFIRMATION_PROMPT,
    RESET_SUCCESS_MESSAGE,
    UNAUTHORIZED_MESSAGE,
    create_telegram_app,
)
from orion.memory.database import ConversationMemory


def test_persistence_across_database_reconnection(tmp_path) -> None:
    """Test (a): Messages persist and can be reloaded across distinct connection instances."""
    db_file = str(tmp_path / "test_memory.db")

    # Instance 1: write messages
    mem1 = ConversationMemory(db_path=db_file)
    mem1.add_message(user_id=1001, role="user", content="Remember my code: 42")
    mem1.add_message(user_id=1001, role="assistant", content="Got it, 42.")

    # Instance 2: reconnect and read
    mem2 = ConversationMemory(db_path=db_file)
    history = mem2.get_recent_messages(user_id=1001, limit=10)

    assert len(history) == 2
    assert history[0] == {"role": "user", "content": "Remember my code: 42"}
    assert history[1] == {"role": "assistant", "content": "Got it, 42."}


@pytest.mark.asyncio
async def test_multiturn_conversation_context() -> None:
    """Test (b): Ollama client constructs payload with structured conversation history."""
    client = OllamaClient(base_url="http://127.0.0.1:11434")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"message": {"content": "Your name is Alice."}}

    history = [
        {"role": "user", "content": "My name is Alice."},
        {"role": "assistant", "content": "Nice to meet you Alice."},
    ]

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        response = await client.generate_response(
            user_message="What is my name?",
            history=history,
        )

        assert response == "Your name is Alice."
        _, kwargs = mock_post.call_args
        payload = kwargs.get("json", {})
        messages = payload.get("messages", [])

        # Check order: system, history..., current user turn
        assert len(messages) == 4
        assert messages[0]["role"] == "system"
        assert messages[1] == history[0]
        assert messages[2] == history[1]
        assert messages[3] == {"role": "user", "content": "What is my name?"}


def test_context_window_limiting(tmp_path) -> None:
    """Test (c): Retrieval strictly respects the configured context window limit."""
    db_file = str(tmp_path / "test_limit.db")
    mem = ConversationMemory(db_path=db_file)

    for i in range(12):
        role = "user" if i % 2 == 0 else "assistant"
        mem.add_message(user_id=2002, role=role, content=f"Message {i}")

    # Request limit of 4
    recent = mem.get_recent_messages(user_id=2002, limit=4)
    assert len(recent) == 4
    # Chronological ordering preserved: oldest first of the last 4
    assert recent[0]["content"] == "Message 8"
    assert recent[1]["content"] == "Message 9"
    assert recent[2]["content"] == "Message 10"
    assert recent[3]["content"] == "Message 11"


@pytest.mark.asyncio
async def test_owner_only_reset_with_confirmation(tmp_path) -> None:
    """Test (d): /reset requires explicit confirmation and enforces owner-only access."""
    db_file = str(tmp_path / "test_reset.db")
    mem = ConversationMemory(db_path=db_file)
    owner_id = 999
    mem.add_message(user_id=owner_id, role="user", content="Keep this")

    token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
    app = create_telegram_app(token=token, owner_id=owner_id, memory=mem)
    reset_handler_func = app.handlers[0][1].callback  # CommandHandler for 'reset'

    # 1. Unauthorized user tries /reset
    update_stranger = MagicMock()
    update_stranger.effective_chat.type = "private"
    update_stranger.effective_user.id = 8888
    update_stranger.effective_message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = ["confirm"]

    await reset_handler_func(update_stranger, context)
    update_stranger.effective_message.reply_text.assert_called_with(UNAUTHORIZED_MESSAGE)
    assert len(mem.get_recent_messages(owner_id)) == 1

    # 2. Owner sends /reset without confirm argument
    update_owner = MagicMock()
    update_owner.effective_chat.type = "private"
    update_owner.effective_user.id = owner_id
    update_owner.effective_message.reply_text = AsyncMock()
    context.args = []

    await reset_handler_func(update_owner, context)
    update_owner.effective_message.reply_text.assert_called_with(
        markdown_to_telegram_html(RESET_CONFIRMATION_PROMPT), parse_mode=ParseMode.HTML
    )
    # History still intact
    assert len(mem.get_recent_messages(owner_id)) == 1

    # 3. Owner sends /reset confirm
    update_owner.effective_message.reply_text.reset_mock()
    context.args = ["confirm"]

    await reset_handler_func(update_owner, context)
    update_owner.effective_message.reply_text.assert_called_with(
        RESET_SUCCESS_MESSAGE, parse_mode=ParseMode.HTML
    )
    # History now cleared
    assert len(mem.get_recent_messages(owner_id)) == 0


def test_second_code_block_formatting_regression() -> None:
    """Test (e): Second code block (including truncated/unclosed block) formats properly."""
    content = (
        "Here is the first snippet:\n"
        "```python\n"
        "x = 1\n"
        "```\n\n"
        "Here is the second snippet:\n"
        "```python \n"
        "y = 2\n"
    )

    formatted = markdown_to_telegram_html(content)
    assert "```python" not in formatted
    assert '<pre><code class="language-python">x = 1</code></pre>' in formatted
    assert '<pre><code class="language-python">y = 2</code></pre>' in formatted
