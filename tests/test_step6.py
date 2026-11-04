"""Unit and integration tests for Step 6: Controlled Windows File Actions."""

from pathlib import Path
import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from telegram import Update
from telegram.constants import ParseMode

from orion.config.settings import Settings
from orion.memory.database import ConversationMemory
from orion.tools.filesystem_actions import (
    MAX_TEXT_FILE_BYTES,
    copy_file,
    create_directory,
    create_text_file,
    move_file,
    rename_file,
)
from orion.tools.registry import ToolRegistry
from orion.tools.router import format_action_proposal, format_tool_result, route_and_execute_tool


@pytest.fixture
def test_env(tmp_path: Path):
    """Fixture providing an isolated directory structure inside allowed directories."""
    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    db_file = tmp_path / "test_step6.db"

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
            "outside": outside_dir,
            "db_path": str(db_file),
            "settings": mock_settings,
        }


# =====================================================================
# 1. Direct Controlled File Operations Tests
# =====================================================================


def test_create_directory_success(test_env):
    target = test_env["allowed"] / "new_folder"
    result = create_directory(str(target))
    assert target.is_dir()
    assert result["name"] == "new_folder"


def test_create_directory_already_exists_fails(test_env):
    target = test_env["allowed"] / "existing_folder"
    target.mkdir()
    with pytest.raises(FileExistsError):
        create_directory(str(target))


def test_create_directory_outside_allowed_fails(test_env):
    target = test_env["outside"] / "unauthorized_folder"
    with pytest.raises(PermissionError):
        create_directory(str(target))


def test_create_text_file_success(test_env):
    target = test_env["allowed"] / "hello.txt"
    result = create_text_file(str(target), "Hello World!")
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == "Hello World!"
    assert result["size_bytes"] == 12


def test_create_text_file_already_exists_fails(test_env):
    target = test_env["allowed"] / "hello.txt"
    target.write_text("existing", encoding="utf-8")
    with pytest.raises(FileExistsError):
        create_text_file(str(target), "overwrite content")
    # Verify original content preserved
    assert target.read_text(encoding="utf-8") == "existing"


def test_create_text_file_exceeds_size_limit_fails(test_env):
    target = test_env["allowed"] / "big.txt"
    large_content = "x" * (MAX_TEXT_FILE_BYTES + 10)
    with pytest.raises(ValueError, match="exceeds maximum allowed size"):
        create_text_file(str(target), large_content)
    assert not target.exists()


def test_copy_file_success(test_env):
    src = test_env["allowed"] / "source.txt"
    src.write_text("sample content", encoding="utf-8")
    dst = test_env["allowed"] / "dest.txt"

    result = copy_file(str(src), str(dst))
    assert dst.is_file()
    assert dst.read_text(encoding="utf-8") == "sample content"
    assert src.is_file()  # source still exists
    assert result["name"] == "dest.txt"


def test_copy_file_destination_exists_fails(test_env):
    src = test_env["allowed"] / "source.txt"
    src.write_text("sample content", encoding="utf-8")
    dst = test_env["allowed"] / "dest.txt"
    dst.write_text("existing target", encoding="utf-8")

    with pytest.raises(FileExistsError):
        copy_file(str(src), str(dst))
    assert dst.read_text(encoding="utf-8") == "existing target"


def test_move_file_success(test_env):
    src = test_env["allowed"] / "to_move.txt"
    src.write_text("move content", encoding="utf-8")
    dst = test_env["allowed"] / "moved.txt"

    result = move_file(str(src), str(dst))
    assert dst.is_file()
    assert not src.exists()
    assert dst.read_text(encoding="utf-8") == "move content"
    assert result["name"] == "moved.txt"


def test_move_file_destination_exists_fails(test_env):
    src = test_env["allowed"] / "to_move.txt"
    src.write_text("move content", encoding="utf-8")
    dst = test_env["allowed"] / "target.txt"
    dst.write_text("existing target", encoding="utf-8")

    with pytest.raises(FileExistsError):
        move_file(str(src), str(dst))
    assert src.is_file()
    assert dst.read_text(encoding="utf-8") == "existing target"


def test_rename_file_success(test_env):
    src = test_env["allowed"] / "old_name.txt"
    src.write_text("rename content", encoding="utf-8")

    result = rename_file(str(src), "new_name.txt")
    assert not src.exists()
    new_path = test_env["allowed"] / "new_name.txt"
    assert new_path.is_file()
    assert result["old_name"] == "old_name.txt"
    assert result["new_name"] == "new_name.txt"


def test_rename_file_invalid_name_fails(test_env):
    src = test_env["allowed"] / "file.txt"
    src.write_text("content", encoding="utf-8")

    with pytest.raises(ValueError, match="simple filename"):
        rename_file(str(src), "../escaped.txt")

    with pytest.raises(ValueError, match="simple filename"):
        rename_file(str(src), "sub/escaped.txt")


def test_copy_file_directory_source_fails(test_env):
    src_dir = test_env["allowed"] / "sub_folder"
    src_dir.mkdir()
    dst = test_env["allowed"] / "dst.txt"
    with pytest.raises(ValueError, match="regular file"):
        copy_file(str(src_dir), str(dst))


def test_copy_file_symlink_source_fails(test_env):
    real_file = test_env["allowed"] / "real.txt"
    real_file.write_text("hello", encoding="utf-8")
    link_file = test_env["allowed"] / "link.txt"
    try:
        link_file.symlink_to(real_file)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks not supported in this environment without privilege")
    with pytest.raises(PermissionError, match="Symlinks and junctions are not permitted"):
        copy_file(str(link_file), str(test_env["allowed"] / "copied_link.txt"))


# =====================================================================
# 2. SQLite Pending Actions & History Tests
# =====================================================================


def test_pending_action_lifecycle(test_env):
    memory = ConversationMemory(test_env["db_path"])
    payload = {"path": "Downloads/test.txt", "content": "123"}

    # 1. Create
    action_id = memory.create_pending_action(
        user_id=12345,
        action_type="create_text_file",
        payload=payload,
        ttl_seconds=300,
    )
    assert len(action_id) == 32

    # 2. Retrieve
    action = memory.get_pending_action(action_id, user_id=12345)
    assert action is not None
    assert action["status"] == "pending"
    assert action["action_type"] == "create_text_file"
    assert action["payload"] == payload

    # 3. Duplicate execution prevention via atomic status update
    first_claim = memory.mark_pending_action_status(action_id, "pending", "confirmed")
    assert first_claim is True

    # Second claim attempt must fail
    second_claim = memory.mark_pending_action_status(action_id, "pending", "confirmed")
    assert second_claim is False

    # 4. Action history logging
    log_id = memory.log_action_history(
        user_id=12345,
        action_type="create_text_file",
        payload=payload,
        status="success",
        result={"path": "Downloads/test.txt"},
    )
    assert log_id > 0

    history = memory.get_recent_action_history(user_id=12345)
    assert len(history) == 1
    assert history[0]["status"] == "success"
    assert history[0]["action_type"] == "create_text_file"


def test_pending_action_expiration(test_env):
    memory = ConversationMemory(test_env["db_path"])
    # Create action with 0 second TTL (already expired)
    action_id = memory.create_pending_action(
        user_id=12345,
        action_type="create_directory",
        payload={"path": "Downloads/folder"},
        ttl_seconds=-1,
    )

    action = memory.get_pending_action(action_id, user_id=12345)
    assert action is not None
    assert action["status"] == "expired"


# =====================================================================
# 3. Router Proposal & Pre-Validation Tests
# =====================================================================


@pytest.mark.asyncio
async def test_router_proposes_write_without_modifying(test_env):
    mock_ollama = MagicMock()
    target_path = test_env["allowed"] / "proposal.txt"
    mock_ollama.generate_structured_json = AsyncMock(
        return_value={
            "tool": "create_text_file",
            "arguments": {"path": str(target_path), "content": "preview data"},
        }
    )

    reg = ToolRegistry()
    res = await route_and_execute_tool("create proposal.txt", ollama_client=mock_ollama, registry=reg)

    assert res is not None
    assert res["handled"] is True
    assert res.get("is_proposal") is True
    assert res["action_type"] == "create_text_file"
    assert not target_path.exists()  # Must NOT create the file yet!


@pytest.mark.asyncio
async def test_router_rejects_unsupported_deletion(test_env):
    mock_ollama = MagicMock()
    mock_ollama.generate_structured_json = AsyncMock(
        return_value={"tool": None, "unsupported": True}
    )

    reg = ToolRegistry()
    res = await route_and_execute_tool("delete my secret.txt", ollama_client=mock_ollama, registry=reg)

    assert res is not None
    assert res["handled"] is True
    assert "strictly prohibited" in res["reply"]


@pytest.mark.asyncio
async def test_router_prevalidation_fails_if_destination_exists(test_env):
    existing = test_env["allowed"] / "existing.txt"
    existing.write_text("data", encoding="utf-8")

    mock_ollama = MagicMock()
    mock_ollama.generate_structured_json = AsyncMock(
        return_value={
            "tool": "create_text_file",
            "arguments": {"path": str(existing), "content": "new"},
        }
    )

    reg = ToolRegistry()
    res = await route_and_execute_tool("create existing.txt", ollama_client=mock_ollama, registry=reg)

    assert res is not None
    assert res["handled"] is True
    assert res.get("is_proposal") is not True  # Does not propose
    assert "already exists" in res["reply"]


# =====================================================================
# 4. Telegram Bot Confirmation Callback Tests
# =====================================================================


@pytest.mark.asyncio
async def test_telegram_callback_confirm_executes_action(test_env):
    from orion.integrations.telegram_bot import create_telegram_app

    memory = ConversationMemory(test_env["db_path"])
    reg = ToolRegistry()

    new_file = test_env["allowed"] / "confirmed.txt"
    action_id = memory.create_pending_action(
        user_id=12345,
        action_type="create_text_file",
        payload={"path": str(new_file), "content": "written on confirmation"},
        ttl_seconds=300,
    )

    app = create_telegram_app(
        token="123456:fake_token",
        owner_id=12345,
        memory=memory,
        tool_registry=reg,
    )

    # Find the CallbackQueryHandler
    callback_handler = None
    for h in app.handlers.get(0, []):
        if h.__class__.__name__ == "CallbackQueryHandler":
            callback_handler = h
            break
    assert callback_handler is not None

    mock_update = MagicMock(spec=Update)
    mock_query = AsyncMock()
    mock_query.data = f"confirm:{action_id}"
    mock_query.from_user.id = 12345
    mock_update.effective_user.id = 12345
    mock_update.callback_query = mock_query

    await callback_handler.callback(mock_update, MagicMock())

    # Verify execution happened
    assert new_file.is_file()
    assert new_file.read_text(encoding="utf-8") == "written on confirmation"

    # Verify pending action status updated
    act = memory.get_pending_action(action_id, user_id=12345)
    assert act["status"] == "confirmed"

    # Verify message was edited with confirmation
    mock_query.edit_message_text.assert_called_once()
    called_text = mock_query.edit_message_text.call_args[0][0]
    assert "Successfully created text file" in called_text


@pytest.mark.asyncio
async def test_telegram_callback_cancel_aborts_action(test_env):
    from orion.integrations.telegram_bot import create_telegram_app

    memory = ConversationMemory(test_env["db_path"])
    reg = ToolRegistry()

    new_file = test_env["allowed"] / "cancelled.txt"
    action_id = memory.create_pending_action(
        user_id=12345,
        action_type="create_text_file",
        payload={"path": str(new_file), "content": "should never be created"},
        ttl_seconds=300,
    )

    app = create_telegram_app(
        token="123456:fake_token",
        owner_id=12345,
        memory=memory,
        tool_registry=reg,
    )

    callback_handler = [h for h in app.handlers.get(0, []) if h.__class__.__name__ == "CallbackQueryHandler"][0]

    mock_update = MagicMock(spec=Update)
    mock_query = AsyncMock()
    mock_query.data = f"cancel:{action_id}"
    mock_query.from_user.id = 12345
    mock_update.effective_user.id = 12345
    mock_update.callback_query = mock_query

    await callback_handler.callback(mock_update, MagicMock())

    # File must NOT be created
    assert not new_file.exists()

    # Status must be cancelled
    act = memory.get_pending_action(action_id, user_id=12345)
    assert act["status"] == "cancelled"

    # Message edited with cancellation notice
    mock_query.edit_message_text.assert_called_once()
    assert "Operation cancelled" in mock_query.edit_message_text.call_args[0][0]


@pytest.mark.asyncio
async def test_telegram_callback_rejects_unauthorized_user(test_env):
    from orion.integrations.telegram_bot import create_telegram_app

    memory = ConversationMemory(test_env["db_path"])
    reg = ToolRegistry()

    new_file = test_env["allowed"] / "unauthorized.txt"
    action_id = memory.create_pending_action(
        user_id=12345,
        action_type="create_text_file",
        payload={"path": str(new_file), "content": "unauthorized attempt"},
        ttl_seconds=300,
    )

    app = create_telegram_app(
        token="123456:fake_token",
        owner_id=12345,
        memory=memory,
        tool_registry=reg,
    )

    callback_handler = [h for h in app.handlers.get(0, []) if h.__class__.__name__ == "CallbackQueryHandler"][0]

    mock_update = MagicMock(spec=Update)
    mock_query = AsyncMock()
    mock_query.data = f"confirm:{action_id}"
    mock_query.from_user.id = 99999  # Attacker ID
    mock_update.effective_user.id = 99999
    mock_update.callback_query = mock_query

    await callback_handler.callback(mock_update, MagicMock())

    # Must reject alert
    mock_query.answer.assert_called_with("Access restricted: unauthorized user or chat.", show_alert=True)
    assert not new_file.exists()
    act = memory.get_pending_action(action_id, user_id=12345)
    assert act["status"] == "pending"


@pytest.mark.asyncio
async def test_telegram_message_handler_sends_proposal_buttons(test_env):
    from orion.integrations.telegram_bot import create_telegram_app

    memory = ConversationMemory(test_env["db_path"])
    reg = ToolRegistry()

    new_file = test_env["allowed"] / "proposed.txt"
    mock_ollama = MagicMock()
    mock_ollama.generate_structured_json = AsyncMock(
        return_value={
            "tool": "create_text_file",
            "arguments": {"path": str(new_file), "content": "hello proposed"},
        }
    )

    app = create_telegram_app(
        token="123456:fake_token",
        owner_id=12345,
        ollama_client=mock_ollama,
        memory=memory,
        tool_registry=reg,
    )

    message_handler = app.handlers[0][-1].callback

    mock_update = MagicMock(spec=Update)
    mock_update.effective_chat.type = "private"
    mock_update.effective_chat.id = 12345
    mock_update.effective_user.id = 12345
    mock_update.effective_message.text = "create proposed.txt with hello proposed"
    mock_update.effective_message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot.send_chat_action = AsyncMock()

    await message_handler(mock_update, context)

    # Must reply with inline keyboard containing Confirm and Cancel buttons
    mock_update.effective_message.reply_text.assert_called_once()
    _, kwargs = mock_update.effective_message.reply_text.call_args
    reply_markup = kwargs.get("reply_markup")
    assert reply_markup is not None
    assert len(reply_markup.inline_keyboard) == 1
    buttons = reply_markup.inline_keyboard[0]
    assert buttons[0].text == "Confirm"
    assert buttons[0].callback_data.startswith("confirm:")
    assert buttons[1].text == "Cancel"
    assert buttons[1].callback_data.startswith("cancel:")

    # File must not yet exist
    assert not new_file.exists()


@pytest.mark.asyncio
async def test_telegram_callback_duplicate_prevented(test_env):
    from orion.integrations.telegram_bot import create_telegram_app

    memory = ConversationMemory(test_env["db_path"])
    reg = ToolRegistry()

    new_file = test_env["allowed"] / "dup.txt"
    action_id = memory.create_pending_action(
        user_id=12345,
        action_type="create_text_file",
        payload={"path": str(new_file), "content": "first"},
        ttl_seconds=300,
    )

    app = create_telegram_app(
        token="123456:fake_token",
        owner_id=12345,
        memory=memory,
        tool_registry=reg,
    )

    callback_handler = [h for h in app.handlers.get(0, []) if h.__class__.__name__ == "CallbackQueryHandler"][0]

    mock_update = MagicMock(spec=Update)
    mock_query = AsyncMock()
    mock_query.data = f"confirm:{action_id}"
    mock_query.from_user.id = 12345
    mock_update.effective_user.id = 12345
    mock_update.callback_query = mock_query

    # First execution succeeds
    await callback_handler.callback(mock_update, MagicMock())
    assert new_file.is_file()

    # Second click on same action must be rejected as already processed
    mock_query.edit_message_text.reset_mock()
    await callback_handler.callback(mock_update, MagicMock())
    mock_query.edit_message_text.assert_called_with("This action was already processed.")


@pytest.mark.asyncio
async def test_telegram_callback_expired_rejected(test_env):
    from orion.integrations.telegram_bot import create_telegram_app

    memory = ConversationMemory(test_env["db_path"])
    reg = ToolRegistry()

    new_file = test_env["allowed"] / "exp.txt"
    action_id = memory.create_pending_action(
        user_id=12345,
        action_type="create_text_file",
        payload={"path": str(new_file), "content": "first"},
        ttl_seconds=-10,  # Expired
    )

    app = create_telegram_app(
        token="123456:fake_token",
        owner_id=12345,
        memory=memory,
        tool_registry=reg,
    )

    callback_handler = [h for h in app.handlers.get(0, []) if h.__class__.__name__ == "CallbackQueryHandler"][0]

    mock_update = MagicMock(spec=Update)
    mock_query = AsyncMock()
    mock_query.data = f"confirm:{action_id}"
    mock_query.from_user.id = 12345
    mock_update.effective_user.id = 12345
    mock_update.callback_query = mock_query

    await callback_handler.callback(mock_update, MagicMock())
    assert not new_file.exists()
    mock_query.edit_message_text.assert_called_with("⏱️ This action has expired. No changes were made.")

