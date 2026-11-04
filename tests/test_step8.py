"""Unit and integration tests for Step 8: Task Management & Execution Control."""

from pathlib import Path
import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from telegram import Update
from telegram.constants import ParseMode

from orion.config.settings import Settings
from orion.core.execution_engine import (
    format_content_preview,
    format_workflow_proposal,
    is_representable_text,
)
from orion.integrations.telegram_bot import UNAUTHORIZED_MESSAGE, create_telegram_app
from orion.memory.database import ConversationMemory
from orion.tools.router import format_action_proposal
from orion.tools.security import prettify_path


@pytest.fixture
def step8_env(tmp_path: Path):
    """Isolated test environment with database and allowed folder."""
    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    db_file = tmp_path / "test_step8.db"

    mock_settings = Settings(
        telegram_bot_token="test_token",
        telegram_owner_id=12345,
        database_path=str(db_file),
        allowed_directories=str(allowed_dir),
    )

    with patch("orion.config.settings.get_settings", return_value=mock_settings), \
         patch("orion.tools.security.get_settings", return_value=mock_settings), \
         patch("orion.memory.database.get_settings", return_value=mock_settings):
        yield {
            "allowed": allowed_dir,
            "db_path": str(db_file),
            "settings": mock_settings,
        }


# =====================================================================
# 1. Status Command Tests (/status)
# =====================================================================


@pytest.mark.asyncio
async def test_status_empty(step8_env):
    memory = ConversationMemory(step8_env["db_path"])
    app = create_telegram_app(token="123:fake", owner_id=12345, memory=memory)

    status_handler = [h for h in app.handlers[0] if getattr(h, "commands", None) == frozenset({"status"})][0].callback

    mock_update = MagicMock(spec=Update)
    mock_update.effective_chat.type = "private"
    mock_update.effective_chat.id = 12345
    mock_update.effective_user.id = 12345
    mock_update.effective_message.reply_text = AsyncMock()

    await status_handler(mock_update, MagicMock())

    mock_update.effective_message.reply_text.assert_called_once()
    reply = mock_update.effective_message.reply_text.call_args[0][0]
    assert "No tasks currently running" in reply
    assert "Pending approval:" in reply and "0" in reply
    assert "None recorded yet" in reply


@pytest.mark.asyncio
async def test_status_with_running_and_pending(step8_env):
    memory = ConversationMemory(step8_env["db_path"])
    # Create running workflow
    wf_running_id = memory.create_execution_workflow(12345, "Running backup", [])
    memory.claim_execution_workflow(wf_running_id)

    # Create pending workflow
    memory.create_execution_workflow(12345, "Pending sync", [])

    app = create_telegram_app(token="123:fake", owner_id=12345, memory=memory)
    status_handler = [h for h in app.handlers[0] if getattr(h, "commands", None) == frozenset({"status"})][0].callback

    mock_update = MagicMock(spec=Update)
    mock_update.effective_chat.type = "private"
    mock_update.effective_chat.id = 12345
    mock_update.effective_user.id = 12345
    mock_update.effective_message.reply_text = AsyncMock()

    await status_handler(mock_update, MagicMock())

    reply = mock_update.effective_message.reply_text.call_args[0][0]
    assert "Running backup" in reply
    assert "Pending approval:" in reply and "1" in reply


@pytest.mark.asyncio
async def test_status_rejects_unauthorized(step8_env):
    memory = ConversationMemory(step8_env["db_path"])
    app = create_telegram_app(token="123:fake", owner_id=12345, memory=memory)
    status_handler = [h for h in app.handlers[0] if getattr(h, "commands", None) == frozenset({"status"})][0].callback

    mock_update = MagicMock(spec=Update)
    mock_update.effective_chat.type = "private"
    mock_update.effective_chat.id = 99999
    mock_update.effective_user.id = 99999
    mock_update.effective_message.reply_text = AsyncMock()

    await status_handler(mock_update, MagicMock())
    mock_update.effective_message.reply_text.assert_called_with(UNAUTHORIZED_MESSAGE)


# =====================================================================
# 2. History Command Tests (/history)
# =====================================================================


@pytest.mark.asyncio
async def test_history_ordering_and_limits(step8_env):
    memory = ConversationMemory(step8_env["db_path"])

    # Log 12 events to test limit of 10
    for i in range(12):
        steps = [{"step_id": 1, "tool": "create_directory", "status": "completed"}]
        wf_id = memory.create_execution_workflow(12345, f"Workflow {i}", steps)
        memory.finish_execution_workflow(wf_id, status="completed", steps=steps)

    app = create_telegram_app(token="123:fake", owner_id=12345, memory=memory)
    history_handler = [h for h in app.handlers[0] if getattr(h, "commands", None) == frozenset({"history"})][0].callback

    mock_update = MagicMock(spec=Update)
    mock_update.effective_chat.type = "private"
    mock_update.effective_chat.id = 12345
    mock_update.effective_user.id = 12345
    mock_update.effective_message.reply_text = AsyncMock()

    await history_handler(mock_update, MagicMock())

    reply = mock_update.effective_message.reply_text.call_args[0][0]
    # Check that latest entries are present
    assert "Workflow 11" in reply
    # Must only display up to 10
    assert "10." in reply
    assert "11." not in reply


@pytest.mark.asyncio
async def test_history_interrupted_status_shown_honestly(step8_env):
    memory = ConversationMemory(step8_env["db_path"])
    wf_id = memory.create_execution_workflow(12345, "Crashed job", [])
    memory.claim_execution_workflow(wf_id)

    # Server restart marks running as interrupted
    restarted_memory = ConversationMemory(step8_env["db_path"])

    app = create_telegram_app(token="123:fake", owner_id=12345, memory=restarted_memory)
    history_handler = [h for h in app.handlers[0] if getattr(h, "commands", None) == frozenset({"history"})][0].callback

    mock_update = MagicMock(spec=Update)
    mock_update.effective_chat.type = "private"
    mock_update.effective_chat.id = 12345
    mock_update.effective_user.id = 12345
    mock_update.effective_message.reply_text = AsyncMock()

    await history_handler(mock_update, MagicMock())

    reply = mock_update.effective_message.reply_text.call_args[0][0]
    assert "Crashed job" in reply
    assert "Interrupted" in reply


# =====================================================================
# 3. Pending Task Cancellation (/cancel)
# =====================================================================


@pytest.mark.asyncio
async def test_cancel_single_pending_workflow(step8_env):
    memory = ConversationMemory(step8_env["db_path"])
    wf_id = memory.create_execution_workflow(12345, "Organize project", [])

    app = create_telegram_app(token="123:fake", owner_id=12345, memory=memory)
    cancel_handler = [h for h in app.handlers[0] if getattr(h, "commands", None) == frozenset({"cancel"})][0].callback

    mock_update = MagicMock(spec=Update)
    mock_update.effective_chat.type = "private"
    mock_update.effective_chat.id = 12345
    mock_update.effective_user.id = 12345
    mock_update.effective_message.reply_text = AsyncMock()

    await cancel_handler(mock_update, MagicMock())

    # Workflow must be cancelled
    wf = memory.get_execution_workflow(wf_id)
    assert wf["status"] == "cancelled"

    reply = mock_update.effective_message.reply_text.call_args[0][0]
    assert "Organize project" in reply
    assert "cancelled" in reply.lower()


@pytest.mark.asyncio
async def test_cancel_multiple_pending_shows_options_and_cancels_all(step8_env):
    memory = ConversationMemory(step8_env["db_path"])
    wf1 = memory.create_execution_workflow(12345, "Task Alpha", [])
    wf2 = memory.create_execution_workflow(12345, "Task Beta", [])

    app = create_telegram_app(token="123:fake", owner_id=12345, memory=memory)
    cancel_handler = [h for h in app.handlers[0] if getattr(h, "commands", None) == frozenset({"cancel"})][0].callback
    callback_handler = [h for h in app.handlers[0] if h.__class__.__name__ == "CallbackQueryHandler"][0].callback

    mock_update = MagicMock(spec=Update)
    mock_update.effective_chat.type = "private"
    mock_update.effective_chat.id = 12345
    mock_update.effective_user.id = 12345
    mock_update.effective_message.reply_text = AsyncMock()

    await cancel_handler(mock_update, MagicMock())

    # Must present buttons for both tasks and a Cancel All button
    _, kwargs = mock_update.effective_message.reply_text.call_args
    reply_markup = kwargs.get("reply_markup")
    assert reply_markup is not None
    assert len(reply_markup.inline_keyboard) == 3  # Task Alpha, Task Beta, Cancel All

    # Trigger Cancel All callback
    mock_query_update = MagicMock(spec=Update)
    mock_query = AsyncMock()
    mock_query.data = "cancel_all_pending"
    mock_query.from_user.id = 12345
    mock_query_update.effective_user.id = 12345
    mock_query_update.callback_query = mock_query

    await callback_handler(mock_query_update, MagicMock())

    assert memory.get_execution_workflow(wf1)["status"] == "cancelled"
    assert memory.get_execution_workflow(wf2)["status"] == "cancelled"
    mock_query.edit_message_text.assert_called_with("Cancelled 2 pending proposal(s). No filesystem changes were made.")


@pytest.mark.asyncio
async def test_cancel_does_not_abort_running_task(step8_env):
    memory = ConversationMemory(step8_env["db_path"])
    wf_id = memory.create_execution_workflow(12345, "Active operation", [])
    memory.claim_execution_workflow(wf_id)

    app = create_telegram_app(token="123:fake", owner_id=12345, memory=memory)
    cancel_handler = [h for h in app.handlers[0] if getattr(h, "commands", None) == frozenset({"cancel"})][0].callback

    mock_update = MagicMock(spec=Update)
    mock_update.effective_chat.type = "private"
    mock_update.effective_chat.id = 12345
    mock_update.effective_user.id = 12345
    mock_update.effective_message.reply_text = AsyncMock()

    await cancel_handler(mock_update, MagicMock())

    reply = mock_update.effective_message.reply_text.call_args[0][0]
    assert "Running filesystem operations cannot be safely cancelled" in reply
    # Must remain running
    assert memory.get_execution_workflow(wf_id)["status"] == "running"


# =====================================================================
# 4. Better Approval Previews Tests
# =====================================================================


def test_approval_preview_shows_complete_details_and_content():
    steps = [
        {
            "step_id": 1,
            "tool": "create_directory",
            "arguments": {"path": "Downloads/Step7Test"},
            "description": "Create folder",
        },
        {
            "step_id": 2,
            "tool": "create_text_file",
            "arguments": {
                "path": "Downloads/Step7Test/welcome.txt",
                "content": "Hello ORION!",
            },
            "description": "Create welcome file",
        },
    ]

    proposal = format_workflow_proposal("Test workflow", steps)

    # Must show full parent location and exact filename
    assert "Create folder: `Step7Test`" in proposal
    assert "Location: `Downloads`" in proposal
    assert "Create text file: `welcome.txt`" in proposal
    assert "Location: `Downloads/Step7Test`" in proposal
    # Must show exact text content for small content
    assert "Hello ORION!" in proposal
    assert "Content (12 bytes)" in proposal


def test_approval_preview_truncation_labeling_for_long_content():
    long_content = "A" * 600
    preview = format_content_preview(long_content, max_preview_bytes=200)

    # Must be clearly labeled with exact byte counts
    assert "preview of first 200 of 600 bytes" in preview
    assert "Truncated preview. Full file will contain 600 bytes." in preview


def test_reject_unprintable_binary_characters():
    binary_content = "Hello\x00World\x01\x02"
    assert is_representable_text(binary_content) is False
    assert is_representable_text("Normal printable text with \n newlines and \t tabs.") is True


def test_prettify_path_masks_private_usernames():
    downloads = prettify_path("C:\\Users\\john_doe\\Downloads\\file.txt")
    # Should resolve with clean alias Downloads
    assert not downloads.startswith("C:\\Users")
