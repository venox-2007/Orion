"""Unit and integration tests for Step 7: Controlled Multi-Step Task Execution."""

from pathlib import Path
import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from telegram import Update
from telegram.constants import ParseMode

from orion.config.settings import Settings
from orion.core.execution_engine import (
    ExecutionPlan,
    ExecutionStep,
    execute_workflow,
    format_workflow_proposal,
    generate_execution_plan,
    simulate_and_validate_workflow,
)
from orion.memory.database import ConversationMemory
from orion.tools.registry import ToolRegistry


@pytest.fixture
def step7_env(tmp_path: Path):
    """Isolated environment with allowed/outside directories and SQLite database."""
    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    db_file = tmp_path / "test_step7.db"

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
# 1. Execution Plan Engine Tests
# =====================================================================


@pytest.mark.asyncio
async def test_generate_execution_plan_success():
    mock_ollama = MagicMock()
    mock_ollama.generate_structured_json = AsyncMock(
        return_value={
            "goal": "Create College Notes folder and move notes.pdf into it",
            "steps": [
                {
                    "step_id": 1,
                    "tool": "create_directory",
                    "arguments": {"path": "Downloads/College Notes"},
                    "description": "Create folder College Notes in Downloads",
                },
                {
                    "step_id": 2,
                    "tool": "move_file",
                    "arguments": {"source": "Downloads/notes.pdf", "destination": "Downloads/College Notes"},
                    "description": "Move notes.pdf into College Notes",
                },
            ],
            "summary": "Create folder and move pdf",
        }
    )

    plan, err = await generate_execution_plan("Create folder and move file", client=mock_ollama)
    assert err is None
    assert plan is not None
    assert len(plan.steps) == 2
    assert plan.steps[0].tool == "create_directory"
    assert plan.steps[1].tool == "move_file"


@pytest.mark.asyncio
async def test_generate_execution_plan_rejects_more_than_3_steps():
    mock_ollama = MagicMock()
    mock_ollama.generate_structured_json = AsyncMock(
        return_value={
            "goal": "Excessive steps",
            "steps": [
                {"step_id": 1, "tool": "create_directory", "arguments": {"path": "A"}, "description": "1"},
                {"step_id": 2, "tool": "create_directory", "arguments": {"path": "B"}, "description": "2"},
                {"step_id": 3, "tool": "create_directory", "arguments": {"path": "C"}, "description": "3"},
                {"step_id": 4, "tool": "create_directory", "arguments": {"path": "D"}, "description": "4"},
            ],
        }
    )

    plan, err = await generate_execution_plan("Do 4 things", client=mock_ollama)
    assert plan is None
    assert "maximum allowed limit of 3 operations" in err


@pytest.mark.asyncio
async def test_generate_execution_plan_rejects_unsupported_actions():
    mock_ollama = MagicMock()
    mock_ollama.generate_structured_json = AsyncMock(
        return_value={"unsupported": True, "reason": "File deletion is strictly prohibited."}
    )

    plan, err = await generate_execution_plan("Delete old files", client=mock_ollama)
    assert plan is None
    assert "deletion is strictly prohibited" in err


@pytest.mark.asyncio
async def test_generate_execution_plan_rejects_ambiguous_requests():
    mock_ollama = MagicMock()
    mock_ollama.generate_structured_json = AsyncMock(
        return_value={"ambiguous": True, "reason": "Please specify the exact filename."}
    )

    plan, err = await generate_execution_plan("Move my files", client=mock_ollama)
    assert plan is None
    assert "ambiguous" in err.lower()


@pytest.mark.asyncio
async def test_generate_execution_plan_rejects_unauthorized_tools():
    mock_ollama = MagicMock()
    mock_ollama.generate_structured_json = AsyncMock(
        return_value={
            "goal": "Run command",
            "steps": [
                {"step_id": 1, "tool": "execute_shell", "arguments": {"cmd": "dir"}, "description": "Run shell"},
            ],
        }
    )

    plan, err = await generate_execution_plan("Run a command", client=mock_ollama)
    assert plan is None
    assert "unauthorized tool" in err


# =====================================================================
# 2. Plan Simulation & Dependency Tests
# =====================================================================


def test_simulation_validates_dependent_workflow(step7_env):
    """Step 1 creates a folder; Step 2 moves an existing file into it."""
    allowed = step7_env["allowed"]
    src_file = allowed / "notes.pdf"
    src_file.write_text("PDF content", encoding="utf-8")

    plan = ExecutionPlan(
        goal="Organize notes",
        steps=[
            ExecutionStep(
                step_id=1,
                tool="create_directory",
                arguments={"path": str(allowed / "College Notes")},
                description="Create folder College Notes",
            ),
            ExecutionStep(
                step_id=2,
                tool="move_file",
                arguments={
                    "source": str(src_file),
                    "destination": str(allowed / "College Notes"),
                },
                description="Move notes.pdf into College Notes",
            ),
        ],
    )

    valid, err, validated_steps = simulate_and_validate_workflow(plan)
    assert valid is True
    assert err == ""
    assert len(validated_steps) == 2

    # Verify simulation DID NOT modify disk!
    assert not (allowed / "College Notes").exists()
    assert src_file.exists()


def test_simulation_rejects_nonexistent_source_file(step7_env):
    allowed = step7_env["allowed"]
    plan = ExecutionPlan(
        goal="Move missing file",
        steps=[
            ExecutionStep(
                step_id=1,
                tool="move_file",
                arguments={
                    "source": str(allowed / "nonexistent.pdf"),
                    "destination": str(allowed / "target.pdf"),
                },
                description="Move missing file",
            ),
        ],
    )

    valid, err, _ = simulate_and_validate_workflow(plan)
    assert valid is False
    assert "File or directory does not exist" in err


def test_simulation_rejects_existing_destination_overwrite(step7_env):
    allowed = step7_env["allowed"]
    existing = allowed / "existing.txt"
    existing.write_text("orig", encoding="utf-8")

    plan = ExecutionPlan(
        goal="Overwrite file",
        steps=[
            ExecutionStep(
                step_id=1,
                tool="create_text_file",
                arguments={"path": str(existing), "content": "new"},
                description="Create file",
            ),
        ],
    )

    valid, err, _ = simulate_and_validate_workflow(plan)
    assert valid is False
    assert "already exists" in err


def test_simulation_rejects_unauthorized_directory(step7_env):
    outside = step7_env["outside"]
    plan = ExecutionPlan(
        goal="Create outside folder",
        steps=[
            ExecutionStep(
                step_id=1,
                tool="create_directory",
                arguments={"path": str(outside / "hacked")},
                description="Create folder outside",
            ),
        ],
    )

    valid, err, _ = simulate_and_validate_workflow(plan)
    assert valid is False
    assert "outside permitted directories" in err


def test_simulation_rejects_path_traversal(step7_env):
    allowed = step7_env["allowed"]
    src = allowed / "file.txt"
    src.write_text("data", encoding="utf-8")

    plan = ExecutionPlan(
        goal="Rename with traversal",
        steps=[
            ExecutionStep(
                step_id=1,
                tool="rename_file",
                arguments={"source": str(src), "new_name": "../escape.txt"},
                description="Rename with traversal",
            ),
        ],
    )

    valid, err, _ = simulate_and_validate_workflow(plan)
    assert valid is False
    assert "directory traversal" in err


# =====================================================================
# 3. Safe Sequential Execution & Failure Halt Tests
# =====================================================================


@pytest.mark.asyncio
async def test_execute_workflow_success_two_steps(step7_env):
    allowed = step7_env["allowed"]
    src = allowed / "notes.pdf"
    src.write_text("PDF content", encoding="utf-8")

    memory = ConversationMemory(step7_env["db_path"])
    reg = ToolRegistry()

    steps = [
        {
            "step_id": 1,
            "tool": "create_directory",
            "arguments": {"path": str(allowed / "College Notes")},
            "description": "Create College Notes",
            "status": "planned",
        },
        {
            "step_id": 2,
            "tool": "move_file",
            "arguments": {
                "source": str(src),
                "destination": str(allowed / "College Notes"),
            },
            "description": "Move notes.pdf into College Notes",
            "status": "planned",
        },
    ]

    wf_id = memory.create_execution_workflow(12345, "Organize notes", steps)
    memory.claim_execution_workflow(wf_id)

    res = await execute_workflow(wf_id, steps, 12345, memory, registry=reg)

    assert res["success"] is True
    assert (allowed / "College Notes").is_dir()
    assert (allowed / "College Notes" / "notes.pdf").is_file()
    assert not src.exists()  # Source was moved

    # Verify database record
    wf = memory.get_execution_workflow(wf_id)
    assert wf["status"] == "completed"
    assert all(s["status"] == "completed" for s in wf["steps"])
    assert "Done!" in res["message"]


@pytest.mark.asyncio
async def test_execute_workflow_step2_failure_halts_step3(step7_env):
    """Failure at step 2 must halt immediately and skip step 3."""
    allowed = step7_env["allowed"]
    src = allowed / "file1.txt"
    src.write_text("one", encoding="utf-8")

    # Destination for step 2 already exists, causing step 2 to fail!
    dest_colliding = allowed / "colliding.txt"
    dest_colliding.write_text("already here", encoding="utf-8")

    memory = ConversationMemory(step7_env["db_path"])
    reg = ToolRegistry()

    step3_target = allowed / "step3_created.txt"

    steps = [
        {
            "step_id": 1,
            "tool": "create_directory",
            "arguments": {"path": str(allowed / "step1_folder")},
            "description": "Create step1 folder",
            "status": "planned",
        },
        {
            "step_id": 2,
            "tool": "copy_file",
            "arguments": {"source": str(src), "destination": str(dest_colliding)},
            "description": "Copy file to existing collision",
            "status": "planned",
        },
        {
            "step_id": 3,
            "tool": "create_text_file",
            "arguments": {"path": str(step3_target), "content": "should never be created"},
            "description": "Step 3 that must be skipped",
            "status": "planned",
        },
    ]

    wf_id = memory.create_execution_workflow(12345, "Three step flow", steps)
    memory.claim_execution_workflow(wf_id)

    res = await execute_workflow(wf_id, steps, 12345, memory, registry=reg)

    assert res["success"] is False
    # Step 1 completed
    assert (allowed / "step1_folder").is_dir()
    assert steps[0]["status"] == "completed"
    # Step 2 failed
    assert steps[1]["status"] == "failed"
    # Step 3 must be SKIPPED and file NOT created!
    assert steps[2]["status"] == "skipped"
    assert not step3_target.exists()

    # Verify database persistence
    wf = memory.get_execution_workflow(wf_id)
    assert wf["status"] == "failed"
    assert wf["steps"][2]["status"] == "skipped"
    assert "remaining action(s) were skipped" in res["message"]


# =====================================================================
# 4. Telegram Workflow Approval & Callback Tests
# =====================================================================


@pytest.mark.asyncio
async def test_telegram_workflow_proposal_and_approve(step7_env):
    from orion.integrations.telegram_bot import create_telegram_app

    allowed = step7_env["allowed"]
    src = allowed / "report.pdf"
    src.write_text("Report data", encoding="utf-8")

    memory = ConversationMemory(step7_env["db_path"])
    reg = ToolRegistry()

    mock_ollama = MagicMock()
    mock_ollama.generate_structured_json = AsyncMock(
        return_value={
            "is_workflow": True,
            "steps": [
                {
                    "step_id": 1,
                    "tool": "create_directory",
                    "arguments": {"path": str(allowed / "Reports")},
                    "description": "Create Reports folder",
                },
                {
                    "step_id": 2,
                    "tool": "move_file",
                    "arguments": {"source": str(src), "destination": str(allowed / "Reports")},
                    "description": "Move report.pdf into Reports",
                },
            ],
        }
    )

    app = create_telegram_app(token="123:fake", owner_id=12345, memory=memory, tool_registry=reg, ollama_client=mock_ollama)

    # 1. Send natural language message proposing workflow
    message_handler = app.handlers[0][-1].callback

    mock_update = MagicMock(spec=Update)
    mock_update.effective_chat.type = "private"
    mock_update.effective_chat.id = 12345
    mock_update.effective_user.id = 12345
    mock_update.effective_message.text = "Create folder Reports and move report.pdf into it"
    mock_update.effective_message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot.send_chat_action = AsyncMock()

    await message_handler(mock_update, context)

    # Verify proposal message sent with Approve All button
    mock_update.effective_message.reply_text.assert_called_once()
    _, kwargs = mock_update.effective_message.reply_text.call_args
    reply_markup = kwargs.get("reply_markup")
    assert reply_markup is not None
    button = reply_markup.inline_keyboard[0][0]
    assert button.text == "Approve All"
    assert button.callback_data.startswith("wf_approve:")

    # Extract workflow_id
    wf_id = button.callback_data.split(":", 1)[1]

    # Verify no disk modifications yet
    assert not (allowed / "Reports").exists()
    assert src.exists()

    # 2. Click Approve All
    callback_handler = [h for h in app.handlers[0] if h.__class__.__name__ == "CallbackQueryHandler"][0]

    mock_query_update = MagicMock(spec=Update)
    mock_query = AsyncMock()
    mock_query.data = f"wf_approve:{wf_id}"
    mock_query.from_user.id = 12345
    mock_query_update.effective_user.id = 12345
    mock_query_update.callback_query = mock_query

    await callback_handler.callback(mock_query_update, MagicMock())

    # Verify execution happened
    assert (allowed / "Reports").is_dir()
    assert (allowed / "Reports" / "report.pdf").is_file()
    assert not src.exists()

    wf = memory.get_execution_workflow(wf_id)
    assert wf["status"] == "completed"


@pytest.mark.asyncio
async def test_telegram_workflow_cancel(step7_env):
    from orion.integrations.telegram_bot import create_telegram_app

    allowed = step7_env["allowed"]
    memory = ConversationMemory(step7_env["db_path"])
    reg = ToolRegistry()

    steps = [
        {
            "step_id": 1,
            "tool": "create_directory",
            "arguments": {"path": str(allowed / "cancelled_folder")},
            "description": "Create folder",
            "status": "planned",
        }
    ]
    wf_id = memory.create_execution_workflow(12345, "Cancel me", steps)

    app = create_telegram_app(token="123:fake", owner_id=12345, memory=memory, tool_registry=reg)
    callback_handler = [h for h in app.handlers[0] if h.__class__.__name__ == "CallbackQueryHandler"][0]

    mock_query_update = MagicMock(spec=Update)
    mock_query = AsyncMock()
    mock_query.data = f"wf_cancel:{wf_id}"
    mock_query.from_user.id = 12345
    mock_query_update.effective_user.id = 12345
    mock_query_update.callback_query = mock_query

    await callback_handler.callback(mock_query_update, MagicMock())

    # Nothing created
    assert not (allowed / "cancelled_folder").exists()
    wf = memory.get_execution_workflow(wf_id)
    assert wf["status"] == "cancelled"
    mock_query.edit_message_text.assert_called_with("Task cancelled. No filesystem changes were made.")


@pytest.mark.asyncio
async def test_telegram_workflow_unauthorized_user_rejected(step7_env):
    from orion.integrations.telegram_bot import create_telegram_app

    memory = ConversationMemory(step7_env["db_path"])
    wf_id = memory.create_execution_workflow(12345, "Secret task", [])

    app = create_telegram_app(token="123:fake", owner_id=12345, memory=memory)
    callback_handler = [h for h in app.handlers[0] if h.__class__.__name__ == "CallbackQueryHandler"][0]

    mock_query_update = MagicMock(spec=Update)
    mock_query = AsyncMock()
    mock_query.data = f"wf_approve:{wf_id}"
    mock_query.from_user.id = 99999  # Attacker
    mock_query_update.effective_user.id = 99999
    mock_query_update.callback_query = mock_query

    await callback_handler.callback(mock_query_update, MagicMock())

    mock_query.answer.assert_called_with("Access restricted: unauthorized user or chat.", show_alert=True)
    wf = memory.get_execution_workflow(wf_id)
    assert wf["status"] == "pending"


@pytest.mark.asyncio
async def test_telegram_workflow_expired_rejected(step7_env):
    from orion.integrations.telegram_bot import create_telegram_app

    memory = ConversationMemory(step7_env["db_path"])
    wf_id = memory.create_execution_workflow(12345, "Expired task", [], ttl_seconds=-10)

    app = create_telegram_app(token="123:fake", owner_id=12345, memory=memory)
    callback_handler = [h for h in app.handlers[0] if h.__class__.__name__ == "CallbackQueryHandler"][0]

    mock_query_update = MagicMock(spec=Update)
    mock_query = AsyncMock()
    mock_query.data = f"wf_approve:{wf_id}"
    mock_query.from_user.id = 12345
    mock_query_update.effective_user.id = 12345
    mock_query_update.callback_query = mock_query

    await callback_handler.callback(mock_query_update, MagicMock())

    mock_query.edit_message_text.assert_called_with("⏱️ This task proposal has expired. No changes were made.")


@pytest.mark.asyncio
async def test_telegram_workflow_duplicate_approval_prevented(step7_env):
    from orion.integrations.telegram_bot import create_telegram_app

    allowed = step7_env["allowed"]
    memory = ConversationMemory(step7_env["db_path"])
    reg = ToolRegistry()

    steps = [
        {
            "step_id": 1,
            "tool": "create_directory",
            "arguments": {"path": str(allowed / "single_run")},
            "description": "Create folder",
            "status": "planned",
        }
    ]
    wf_id = memory.create_execution_workflow(12345, "One-shot", steps)

    app = create_telegram_app(token="123:fake", owner_id=12345, memory=memory, tool_registry=reg)
    callback_handler = [h for h in app.handlers[0] if h.__class__.__name__ == "CallbackQueryHandler"][0]

    mock_query_update = MagicMock(spec=Update)
    mock_query = AsyncMock()
    mock_query.data = f"wf_approve:{wf_id}"
    mock_query.from_user.id = 12345
    mock_query_update.effective_user.id = 12345
    mock_query_update.callback_query = mock_query

    # First click executes
    await callback_handler.callback(mock_query_update, MagicMock())
    assert (allowed / "single_run").is_dir()

    # Second click rejected
    mock_query.edit_message_text.reset_mock()
    await callback_handler.callback(mock_query_update, MagicMock())
    mock_query.edit_message_text.assert_called_with("⚠️ This workflow was already processed.")


def test_interrupted_workflow_recovery_on_restart(step7_env):
    """If ORION restarts with a workflow in 'running' status, it must be marked 'interrupted'."""
    memory = ConversationMemory(step7_env["db_path"])
    wf_id = memory.create_execution_workflow(12345, "Interrupted flow", [])

    # Simulate workflow was claimed and left 'running' when server crashed
    memory.claim_execution_workflow(wf_id)
    wf_before = memory.get_execution_workflow(wf_id)
    assert wf_before["status"] == "running"

    # Simulate restart by re-initializing ConversationMemory
    recovered_memory = ConversationMemory(step7_env["db_path"])
    wf_after = recovered_memory.get_execution_workflow(wf_id)
    assert wf_after["status"] == "interrupted"
