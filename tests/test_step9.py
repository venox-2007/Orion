"""Focused tests for Step 9: Controlled Windows Application Launching."""

import asyncio
from pathlib import Path
import subprocess
import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from orion.config.settings import Settings
from orion.memory.database import ConversationMemory
from orion.tools.app_launcher import (
    APPROVED_APPLICATIONS,
    BLOCKED_APPLICATIONS,
    AppDefinition,
    get_approved_apps,
    is_app_available,
    is_app_blocked,
    launch_application,
    resolve_app_alias,
)
from orion.tools.registry import ToolRegistry
from orion.tools.router import (
    check_deterministic_app_launch,
    format_action_proposal,
    format_tool_result,
    route_and_execute_tool,
)


@pytest.fixture
def step9_env(tmp_path: Path):
    """Fixture providing isolated database and mock settings."""
    db_file = tmp_path / "test_step9.db"
    settings = Settings(
        telegram_bot_token="test_token",
        telegram_owner_id=12345,
        database_path=str(db_file),
    )
    with patch("orion.config.settings.get_settings", return_value=settings), \
         patch("orion.memory.database.get_settings", return_value=settings):
        memory = ConversationMemory(db_path=str(db_file))
        yield {
            "db_path": str(db_file),
            "settings": settings,
            "memory": memory,
            "tmp_path": tmp_path,
        }


# 1. Exact approved allowlist contains 20 apps
def test_exact_allowlist_contains_20_apps():
    apps = get_approved_apps()
    assert len(apps) == 20

    expected_ids = {
        # Productivity (7)
        "word",
        "excel",
        "powerpoint",
        "notepad",
        "calculator",
        "sticky_notes",
        "file_explorer",
        # Development & AI (4)
        "vscode",
        "antigravity_ide",
        "chatgpt",
        "claude",
        # Browsers & Communication (5)
        "edge",
        "comet",
        "whatsapp",
        "teams",
        "zoom",
        # Media & Utilities (4)
        "vlc",
        "paint",
        "snipping_tool",
        "winrar",
    }
    assert set(apps.keys()) == expected_ids

    # Check categories
    categories = {app.category for app in apps.values()}
    assert categories == {
        "Productivity",
        "Development & AI",
        "Browsers & Communication",
        "Media & Utilities",
    }
    assert sum(1 for a in apps.values() if a.category == "Productivity") == 7
    assert sum(1 for a in apps.values() if a.category == "Development & AI") == 4
    assert sum(1 for a in apps.values() if a.category == "Browsers & Communication") == 5
    assert sum(1 for a in apps.values() if a.category == "Media & Utilities") == 4


# 2. All unapproved apps are rejected
@pytest.mark.asyncio
async def test_all_unapproved_apps_are_rejected():
    # Known blocked applications
    blocked_examples = ["chrome", "steam", "discord", "powershell", "cmd", "terminal", "epic games", "git bash"]
    for b in blocked_examples:
        is_blocked, reason = is_app_blocked(b)
        assert is_blocked is True
        assert len(reason) > 0

        res = check_deterministic_app_launch(f"open {b}")
        assert res is not None
        assert res["handled"] is True
        assert "not in the approved" in res["reply"]

    # Unknown unapproved application
    res_unknown = check_deterministic_app_launch("open spotify")
    assert res_unknown is not None
    assert res_unknown["handled"] is True
    assert "not in the approved" in res_unknown["reply"]

    # Direct launch_application call with unapproved ID fails
    res_launch = launch_application("unapproved_id")
    assert res_launch["success"] is False
    assert "not in the approved" in res_launch["error"]


# 3. Missing approved apps are unavailable
def test_missing_approved_apps_are_unavailable():
    with patch.object(APPROVED_APPLICATIONS["word"], "path_resolver", return_value=Path(r"C:\NonExistent\word.exe")):
        is_avail, msg, _ = is_app_available("word")
        assert is_avail is False
        assert "not found" in msg.lower() or "does not exist" in msg.lower()

        res = launch_application("word")
        assert res["success"] is False
        assert "unavailable" in res["error"].lower()


# 4. Valid Win32 executable resolution
def test_valid_win32_executable_resolution(tmp_path):
    fake_exe = tmp_path / "WINWORD.EXE"
    fake_exe.write_text("binary", encoding="utf-8")

    with patch.object(APPROVED_APPLICATIONS["word"], "path_resolver", return_value=fake_exe):
        with patch("orion.tools.app_launcher._get_trusted_win32_roots", return_value=[tmp_path]):
            is_avail, msg, canonical = is_app_available("word")
            assert is_avail is True
            assert canonical == fake_exe.resolve()


# 5. Unsafe executable path rejected
def test_unsafe_executable_path_rejected(tmp_path):
    # Executable in Downloads
    downloads_dir = tmp_path / "Downloads"
    downloads_dir.mkdir()
    fake_exe = downloads_dir / "WINWORD.EXE"
    fake_exe.write_text("binary", encoding="utf-8")

    with patch.object(APPROVED_APPLICATIONS["word"], "path_resolver", return_value=fake_exe):
        with patch("orion.tools.app_launcher._get_trusted_win32_roots", return_value=[tmp_path]):
            is_avail, msg, _ = is_app_available("word")
            assert is_avail is False
            assert "untrusted directory" in msg.lower()


# 6. Registered per-user paths handled safely
def test_registered_per_user_paths_handled_safely(tmp_path):
    programs_dir = tmp_path / "Programs" / "Microsoft VS Code"
    programs_dir.mkdir(parents=True)
    fake_code = programs_dir / "Code.exe"
    fake_code.write_text("binary", encoding="utf-8")

    with patch.object(APPROVED_APPLICATIONS["vscode"], "path_resolver", return_value=fake_code):
        with patch("orion.tools.app_launcher._get_trusted_win32_roots", return_value=[tmp_path / "Programs"]):
            is_avail, msg, canonical = is_app_available("vscode")
            assert is_avail is True
            assert canonical == fake_code.resolve()


# 7. Packaged AppUserModelID validation
def test_packaged_app_user_model_id_validation():
    calc = APPROVED_APPLICATIONS["calculator"]
    assert calc.launch_type == "packaged"
    with patch("orion.tools.app_launcher._check_packaged_app_installed", return_value=True):
        is_avail, msg, target = is_app_available("calculator")
        assert is_avail is True
        assert target == "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"

    # Reject if package not installed
    with patch("orion.tools.app_launcher._check_packaged_app_installed", return_value=False):
        is_avail, msg, target = is_app_available("calculator")
        assert is_avail is False
        assert "not installed or available" in msg


# 8. No raw shell commands or URI injection
def test_no_raw_shell_commands_or_uri_injection():
    malicious_inputs = [
        "word; rm -rf /",
        "calculator & notepad.exe",
        "shell:Personal",
        "file:///C:/Windows/System32/cmd.exe",
        "https://malicious.site",
    ]
    for inp in malicious_inputs:
        alias = resolve_app_alias(inp)
        assert alias is None

        res = launch_application(inp)
        assert res["success"] is False
        assert "not in the approved" in res["error"]


# 9. Win32 launches with shell=False
def test_win32_launches_with_shell_false(tmp_path):
    fake_exe = tmp_path / "notepad.exe"
    fake_exe.write_text("bin", encoding="utf-8")

    with patch.object(APPROVED_APPLICATIONS["notepad"], "path_resolver", return_value=fake_exe):
        with patch("orion.tools.app_launcher._get_trusted_win32_roots", return_value=[tmp_path]):
            with patch("subprocess.Popen") as mock_popen:
                res = launch_application("notepad")
                assert res["success"] is True
                assert mock_popen.called
                _, kwargs = mock_popen.call_args
                assert kwargs.get("shell") is False
                assert kwargs.get("close_fds") is True


# 10. No LLM-generated launch arguments
def test_no_llm_generated_launch_arguments(tmp_path):
    fake_exe = tmp_path / "WINWORD.EXE"
    fake_exe.write_text("bin", encoding="utf-8")

    with patch.object(APPROVED_APPLICATIONS["word"], "path_resolver", return_value=fake_exe):
        with patch("orion.tools.app_launcher._get_trusted_win32_roots", return_value=[tmp_path]):
            with patch("subprocess.Popen") as mock_popen:
                launch_application("word")
                args, _ = mock_popen.call_args
                cmd_vector = args[0]
                assert cmd_vector == [str(fake_exe.resolve())]


# 11. No execution before confirmation
@pytest.mark.asyncio
async def test_no_execution_before_confirmation():
    with patch("subprocess.Popen") as mock_popen:
        with patch("orion.tools.app_launcher.is_app_available", return_value=(True, "Available", Path("C:/fake.exe"))):
            res = await route_and_execute_tool("Orion, open VS Code.")
            assert res is not None
            assert res.get("is_proposal") is True
            assert res.get("action_type") == "launch_application"
            assert res["arguments"] == {"app_id": "vscode"}
            assert not mock_popen.called


# 12. Owner-only approval
@pytest.mark.asyncio
async def test_owner_only_approval(step9_env):
    from orion.integrations.telegram_bot import create_telegram_app

    memory = step9_env["memory"]
    owner_id = 12345
    token = "12345:mock_token"
    app = create_telegram_app(token=token, owner_id=owner_id, memory=memory)

    update = MagicMock()
    update.effective_chat.type = "private"
    update.effective_user.id = 99999  # Unauthorized user
    query = MagicMock()
    query.data = "confirm:action_123"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update.callback_query = query

    with patch("subprocess.Popen") as mock_popen:
        for handler in app.handlers.get(0, []):
            if type(handler).__name__ == "CallbackQueryHandler":
                await handler.callback(update, MagicMock())
                break

        query.answer.assert_called_with("Access restricted: unauthorized user or chat.", show_alert=True)
        assert not mock_popen.called


# 13. Expired or cancelled approval rejected
@pytest.mark.asyncio
async def test_expired_or_cancelled_approval_rejected(step9_env):
    from orion.integrations.telegram_bot import create_telegram_app

    memory = step9_env["memory"]
    owner_id = 12345
    token = "12345:mock_token"
    app = create_telegram_app(token=token, owner_id=owner_id, memory=memory)

    # 1. Cancelled action
    action_id_cancel = memory.create_pending_action(
        user_id=owner_id,
        action_type="launch_application",
        payload={"app_id": "vscode"},
        ttl_seconds=300,
    )
    memory.mark_pending_action_status(action_id_cancel, from_status="pending", to_status="cancelled")

    update_cancel = MagicMock()
    update_cancel.effective_chat.type = "private"
    update_cancel.effective_user.id = owner_id
    query_cancel = MagicMock()
    query_cancel.data = f"confirm:{action_id_cancel}"
    query_cancel.answer = AsyncMock()
    query_cancel.edit_message_text = AsyncMock()
    update_cancel.callback_query = query_cancel

    with patch("subprocess.Popen") as mock_popen:
        for handler in app.handlers.get(0, []):
            if type(handler).__name__ == "CallbackQueryHandler":
                await handler.callback(update_cancel, MagicMock())
                break
        query_cancel.edit_message_text.assert_called_with("This action was already processed.")
        assert not mock_popen.called

    # 2. Expired action
    action_id_expire = memory.create_pending_action(
        user_id=owner_id,
        action_type="launch_application",
        payload={"app_id": "vscode"},
        ttl_seconds=0,
    )
    time.sleep(0.01)

    update_expire = MagicMock()
    update_expire.effective_chat.type = "private"
    update_expire.effective_user.id = owner_id
    query_expire = MagicMock()
    query_expire.data = f"confirm:{action_id_expire}"
    query_expire.answer = AsyncMock()
    query_expire.edit_message_text = AsyncMock()
    update_expire.callback_query = query_expire

    with patch("subprocess.Popen") as mock_popen:
        for handler in app.handlers.get(0, []):
            if type(handler).__name__ == "CallbackQueryHandler":
                await handler.callback(update_expire, MagicMock())
                break
        query_expire.edit_message_text.assert_called_with("⏱️ This action has expired. No changes were made.")
        assert not mock_popen.called


# 14. Duplicate execution prevented
@pytest.mark.asyncio
async def test_duplicate_execution_prevented(step9_env):
    from orion.integrations.telegram_bot import create_telegram_app

    memory = step9_env["memory"]
    owner_id = 12345
    token = "12345:mock_token"
    app = create_telegram_app(token=token, owner_id=owner_id, memory=memory)

    action_id = memory.create_pending_action(
        user_id=owner_id,
        action_type="launch_application",
        payload={"app_id": "vscode"},
        ttl_seconds=300,
    )

    update = MagicMock()
    update.effective_chat.type = "private"
    update.effective_user.id = owner_id
    query = MagicMock()
    query.data = f"confirm:{action_id}"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update.callback_query = query

    with patch("orion.tools.app_launcher.is_app_available", return_value=(True, "Available", Path("C:/fake.exe"))):
        with patch("subprocess.Popen") as mock_popen:
            callback_handler = None
            for handler in app.handlers.get(0, []):
                if type(handler).__name__ == "CallbackQueryHandler":
                    callback_handler = handler.callback
                    break
            assert callback_handler is not None

            # First confirmation: succeeds
            await callback_handler(update, MagicMock())
            assert mock_popen.call_count == 1

            # Second confirmation: duplicate prevented
            await callback_handler(update, MagicMock())
            assert mock_popen.call_count == 1
            query.edit_message_text.assert_called_with("This action was already processed.")


# 15. Revalidation before launch
@pytest.mark.asyncio
async def test_revalidation_before_launch(step9_env):
    from orion.integrations.telegram_bot import create_telegram_app

    memory = step9_env["memory"]
    owner_id = 12345
    token = "12345:mock_token"
    app = create_telegram_app(token=token, owner_id=owner_id, memory=memory)

    action_id = memory.create_pending_action(
        user_id=owner_id,
        action_type="launch_application",
        payload={"app_id": "word"},
        ttl_seconds=300,
    )

    update = MagicMock()
    update.effective_chat.type = "private"
    update.effective_user.id = owner_id
    query = MagicMock()
    query.data = f"confirm:{action_id}"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update.callback_query = query

    # Before confirmation, app became unavailable
    with patch("orion.integrations.telegram_bot.is_app_available", return_value=(False, "Executable not found", None)):
        with patch("subprocess.Popen") as mock_popen:
            for handler in app.handlers.get(0, []):
                if type(handler).__name__ == "CallbackQueryHandler":
                    await handler.callback(update, MagicMock())
                    break

            assert not mock_popen.called
            # Action history recorded as failed
            history = memory.get_unified_history(owner_id, limit=5)
            assert len(history) == 1
            assert history[0]["status"] == "Failed to launch"
            assert history[0]["summary"] == "Microsoft Word"


# 16. /apps lists only installed approved apps
@pytest.mark.asyncio
async def test_apps_command_lists_only_installed_approved_apps(step9_env):
    from orion.integrations.telegram_bot import create_telegram_app

    memory = step9_env["memory"]
    owner_id = 12345
    token = "12345:mock_token"
    app = create_telegram_app(token=token, owner_id=owner_id, memory=memory)

    update = MagicMock()
    update.effective_chat.type = "private"
    update.effective_user.id = owner_id
    msg = MagicMock()
    msg.reply_text = AsyncMock()
    update.effective_message = msg

    def mock_avail(app_id):
        if app_id in ("vscode", "notepad"):
            return True, "Available", Path("fake")
        return False, "Not installed", None

    with patch("orion.integrations.telegram_bot.is_app_available", side_effect=mock_avail):
        for handler in app.handlers.get(0, []):
            if type(handler).__name__ == "CommandHandler" and "apps" in getattr(handler, "commands", []):
                await handler.callback(update, MagicMock())
                break

        msg.reply_text.assert_called()
        call_text = msg.reply_text.call_args[0][0]
        assert "ORION — Available Applications" in call_text
        assert "Visual Studio Code" in call_text
        assert "Notepad" in call_text
        assert "Unavailable" in call_text
        assert "Google Chrome" not in call_text
        assert "Steam" not in call_text
        assert "Discord" not in call_text
        assert "vscode" not in call_text
        assert ".exe" not in call_text.lower()


# 17. /history records launch outcomes
def test_history_records_launch_outcomes(step9_env):
    memory = step9_env["memory"]
    owner_id = 12345

    # 1. Successful launch
    memory.log_action_history(
        user_id=owner_id,
        action_type="launch_application",
        payload={"app_id": "vscode", "display_name": "Visual Studio Code"},
        status="success",
    )

    # 2. Cancelled launch
    memory.log_action_history(
        user_id=owner_id,
        action_type="launch_application",
        payload={"app_id": "calculator", "display_name": "Calculator"},
        status="cancelled",
    )

    # 3. Failed launch
    memory.log_action_history(
        user_id=owner_id,
        action_type="launch_application",
        payload={"app_id": "word", "display_name": "Microsoft Word"},
        status="failed",
    )

    history = memory.get_unified_history(owner_id, limit=10)
    assert len(history) == 3

    failed_item = next(h for h in history if "Word" in h["summary"])
    assert failed_item["summary"] == "Microsoft Word"
    assert failed_item["status"] == "Failed to launch"

    cancelled_item = next(h for h in history if "Calculator" in h["summary"])
    assert cancelled_item["summary"] == "Calculator"
    assert cancelled_item["status"] == "Cancelled"

    success_item = next(h for h in history if "Visual Studio Code" in h["summary"])
    assert success_item["summary"] == "Opened Visual Studio Code"
    assert success_item["status"] == "Launch requested"

    # Latest outcome
    latest = memory.get_latest_execution_outcome(owner_id)
    assert latest is not None
    assert latest["summary"] == "Microsoft Word"
    assert latest["status"] == "Failed to launch"


# 18. Existing Step 6–8 workflows still work
def test_existing_step6_to_8_workflows_still_work():
    registry = ToolRegistry()

    assert registry.is_registered("create_directory")
    assert registry.is_registered("create_text_file")
    assert registry.is_registered("copy_file")
    assert registry.is_registered("move_file")
    assert registry.is_registered("rename_file")
    assert registry.is_registered("launch_application")

    assert registry.is_registered("get_battery_status")
    assert registry.is_registered("get_system_resources")
    assert registry.is_registered("list_files")
    assert registry.is_registered("find_files")
    assert registry.is_registered("get_file_info")
