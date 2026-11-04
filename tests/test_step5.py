"""Focused tests for Step 5: safe read-only Windows tools and routing."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from telegram.constants import ParseMode

from orion.config.settings import Settings
from orion.integrations.telegram_bot import (
    UNAUTHORIZED_MESSAGE,
    create_telegram_app,
)
from orion.memory.database import ConversationMemory
from orion.tools.filesystem import find_files, get_file_info, list_files
from orion.tools.registry import ToolRegistry, default_tool_registry
from orion.tools.router import format_tool_result, route_and_execute_tool
from orion.tools.security import validate_safe_path
from orion.tools.system import get_battery_status, get_system_resources


def test_tool_selection_and_argument_validation() -> None:
    """Test (1): Tool selection, execution, and argument validation."""
    registry = ToolRegistry()

    # Valid invocation with empty arguments
    res_battery = registry.execute("get_battery_status", {})
    assert res_battery["success"] is True
    assert "data" in res_battery

    # Valid invocation of system resources
    res_sys = registry.execute("get_system_resources", {})
    assert res_sys["success"] is True
    assert "cpu_percent" in res_sys["data"]

    # Invalid arguments for list_files (empty directory)
    res_invalid = registry.execute("list_files", {"directory": ""})
    assert res_invalid["success"] is False
    assert "Invalid arguments" in res_invalid["error"]


def test_allowed_and_denied_file_paths(tmp_path) -> None:
    """Test (2): Canonical path validation, directory traversal blocking, and symlink/junction escapes."""
    allowed_dir = tmp_path / "permitted"
    allowed_dir.mkdir()
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()

    # 1. Default disabled state (empty allowed_directories)
    with patch("orion.tools.security.get_settings", return_value=Settings(allowed_directories="")):
        with pytest.raises(PermissionError) as exc:
            validate_safe_path(str(allowed_dir))
        assert "Filesystem access is disabled" in str(exc.value)

    # 2. Configured allowed directory
    custom_settings = Settings(allowed_directories=str(allowed_dir))
    with patch("orion.tools.security.get_settings", return_value=custom_settings):
        # Valid access inside allowed folder
        valid_file = allowed_dir / "note.txt"
        valid_file.write_text("hello", encoding="utf-8")
        resolved = validate_safe_path(str(valid_file))
        assert resolved == valid_file.resolve()

        # Denied access outside allowed folder
        forbidden_file = secret_dir / "password.txt"
        forbidden_file.write_text("secret", encoding="utf-8")
        with pytest.raises(PermissionError):
            validate_safe_path(str(forbidden_file))

        # Traversal escape attempt (allowed/../secret)
        traversal_path = allowed_dir / ".." / "secret" / "password.txt"
        with pytest.raises(PermissionError):
            validate_safe_path(str(traversal_path))

        # Symlink escape attempt pointing outside allowed
        try:
            symlink_path = allowed_dir / "escape_link"
            symlink_path.symlink_to(secret_dir, target_is_directory=True)
            with pytest.raises(PermissionError):
                validate_safe_path(str(symlink_path / "password.txt"))
        except (OSError, NotImplementedError):
            # Pass if running on Windows without symlink privileges
            pass


def test_readonly_tool_outputs(tmp_path) -> None:
    """Test (3): Read-only tool outputs without reading or modifying file contents."""
    allowed_dir = tmp_path / "safe"
    allowed_dir.mkdir()
    test_file = allowed_dir / "report.pdf"
    test_file.write_bytes(b"mock binary content")

    custom_settings = Settings(allowed_directories=str(allowed_dir))
    with patch("orion.tools.security.get_settings", return_value=custom_settings):
        # 1. list_files
        res_list = list_files(str(allowed_dir))
        assert res_list["count"] == 1
        assert res_list["items"][0]["name"] == "report.pdf"

        # 2. find_files
        res_find = find_files("report", str(allowed_dir))
        assert res_find["count"] == 1
        assert res_find["matches"][0]["name"] == "report.pdf"

        # 3. get_file_info (metadata only, no content)
        res_info = get_file_info(str(test_file))
        assert res_info["name"] == "report.pdf"
        assert res_info["size_bytes"] == len(b"mock binary content")
        assert "modified_at" in res_info
        assert "content" not in res_info


def test_unsupported_tool_rejection() -> None:
    """Test (4): Rejection of unapproved tools or destructive operations."""
    registry = ToolRegistry()

    # Direct execution of unapproved tool
    res = registry.execute("delete_file", {"path": "C:\\test"})
    assert res["success"] is False
    assert "not recognized or permitted" in res["error"]

    res_cmd = registry.execute("exec_shell", {"cmd": "dir"})
    assert res_cmd["success"] is False


@pytest.mark.asyncio
async def test_telegram_tool_message_flow(tmp_path) -> None:
    """Test (5): Owner-only natural language tool routing, formatting, and rejection for strangers."""
    db_file = str(tmp_path / "test_flow.db")
    mem = ConversationMemory(db_path=db_file)
    owner_id = 1111
    token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"

    # Mock Ollama returning tool call for resources
    mock_ollama = MagicMock()
    mock_ollama.generate_structured_json = AsyncMock(
        return_value={"tool": "get_system_resources", "arguments": {}}
    )
    mock_ollama.generate_response = AsyncMock(return_value="Normal reply")

    app = create_telegram_app(token=token, owner_id=owner_id, ollama_client=mock_ollama, memory=mem)
    message_handler_func = app.handlers[0][-1].callback

    context = MagicMock()
    context.bot.send_chat_action = AsyncMock()

    # 1. Authorized owner asks for system resources
    update_owner = MagicMock()
    update_owner.effective_chat.type = "private"
    update_owner.effective_chat.id = owner_id
    update_owner.effective_user.id = owner_id
    update_owner.effective_message.reply_text = AsyncMock()
    update_owner.effective_message.text = "How much RAM am I using?"

    await message_handler_func(update_owner, context)
    update_owner.effective_message.reply_text.assert_called_once()
    reply = update_owner.effective_message.reply_text.call_args[0][0]
    assert "System Resources" in reply
    assert "RAM" in reply
    assert "{" not in reply  # No raw JSON returned to user

    # 2. Unauthorized stranger asks for battery
    update_stranger = MagicMock()
    update_stranger.effective_chat.type = "private"
    update_stranger.effective_user.id = 8888
    update_stranger.effective_message.reply_text = AsyncMock()
    update_stranger.effective_message.text = "Check battery"

    await message_handler_func(update_stranger, context)
    update_stranger.effective_message.reply_text.assert_called_with(UNAUTHORIZED_MESSAGE)
