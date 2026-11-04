"""Focused tests for Step 4: persistent AI task planning and commands."""

from unittest.mock import AsyncMock, MagicMock
import pytest
from telegram.constants import ParseMode

from orion.core.models import TaskPlan, TaskStep
from orion.core.planner import generate_task_plan
from orion.integrations.telegram_bot import (
    UNAUTHORIZED_MESSAGE,
    create_telegram_app,
)
from orion.memory.database import ConversationMemory


@pytest.mark.asyncio
async def test_valid_structured_plan_generation() -> None:
    """Test (a): Valid structured plan generation and validation via Pydantic."""
    mock_client = MagicMock()
    mock_client.generate_structured_json = AsyncMock(
        return_value={
            "goal": "Build a doghouse",
            "steps": [
                {"id": 1, "description": "Gather wood and tools", "status": "proposed"},
                {"id": 2, "description": "Construct the frame", "status": "proposed"},
                {"id": 3, "description": "Attach roof and paint", "status": "proposed"},
            ],
        }
    )

    plan = await generate_task_plan("Build a doghouse", client=mock_client)
    assert plan is not None
    assert isinstance(plan, TaskPlan)
    assert plan.goal == "Build a doghouse"
    assert len(plan.steps) == 3
    assert plan.steps[0].id == 1
    assert plan.steps[0].description == "Gather wood and tools"


@pytest.mark.asyncio
async def test_invalid_model_output_handling() -> None:
    """Test (b): Graceful handling of empty, malformed, or out-of-bounds model outputs."""
    mock_client = MagicMock()

    # 1. Ollama returns None (timeout/failure)
    mock_client.generate_structured_json = AsyncMock(return_value=None)
    plan = await generate_task_plan("Do something", client=mock_client)
    assert plan is None

    # 2. Ollama returns malformed payload without steps
    mock_client.generate_structured_json = AsyncMock(return_value={"goal": "No steps"})
    plan = await generate_task_plan("Do something", client=mock_client)
    assert plan is None

    # 3. Ollama returns > 8 steps (violates schema)
    too_many_steps = [{"id": i, "description": f"Step {i}", "status": "proposed"} for i in range(1, 10)]
    mock_client.generate_structured_json = AsyncMock(
        return_value={"goal": "Too long plan", "steps": too_many_steps}
    )
    plan = await generate_task_plan("Do something", client=mock_client)
    assert plan is None

    # 4. Empty goal rejected immediately without calling LLM
    mock_client.generate_structured_json.reset_mock()
    plan_empty = await generate_task_plan("   ", client=mock_client)
    assert plan_empty is None
    mock_client.generate_structured_json.assert_not_called()


def test_plan_persistence_across_reconnection(tmp_path) -> None:
    """Test (c): Action plans persist in SQLite across distinct memory instances."""
    db_file = str(tmp_path / "test_plans.db")
    owner_id = 777

    mem1 = ConversationMemory(db_path=db_file)
    steps = [
        {"id": 1, "description": "Check oil level", "status": "proposed"},
        {"id": 2, "description": "Replace filter", "status": "proposed"},
    ]
    plan_id = mem1.save_task_plan(owner_id, "Car maintenance", steps)

    # Reconnect using second instance
    mem2 = ConversationMemory(db_path=db_file)
    loaded = mem2.get_task_plan(owner_id, plan_id)

    assert loaded is not None
    assert loaded["id"] == plan_id
    assert loaded["goal"] == "Car maintenance"
    assert len(loaded["steps"]) == 2
    assert loaded["steps"][0]["description"] == "Check oil level"


def test_owner_only_plan_access(tmp_path) -> None:
    """Test (d): Plans are strictly private and cannot be read by another user."""
    db_file = str(tmp_path / "test_access.db")
    owner_id = 123
    stranger_id = 999

    mem = ConversationMemory(db_path=db_file)
    plan_id = mem.save_task_plan(
        owner_id, "Secret goal", [{"id": 1, "description": "Top secret", "status": "proposed"}]
    )

    # Stranger cannot get the plan by ID
    assert mem.get_task_plan(stranger_id, plan_id) is None
    # Stranger cannot list the plan
    assert len(mem.get_recent_task_plans(stranger_id)) == 0

    # Owner can get and list the plan
    assert mem.get_task_plan(owner_id, plan_id) is not None
    assert len(mem.get_recent_task_plans(owner_id)) == 1


@pytest.mark.asyncio
async def test_telegram_plan_commands(tmp_path) -> None:
    """Test (e): Telegram /plan, /tasks, and /task command formatting and owner routing."""
    db_file = str(tmp_path / "test_tg_plans.db")
    mem = ConversationMemory(db_path=db_file)
    owner_id = 500
    token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"

    mock_ollama = MagicMock()
    mock_ollama.generate_structured_json = AsyncMock(
        return_value={
            "goal": "Write unit tests",
            "steps": [
                {"id": 1, "description": "Identify edge cases", "status": "proposed"},
                {"id": 2, "description": "Write pytest functions", "status": "proposed"},
            ],
        }
    )

    app = create_telegram_app(token=token, owner_id=owner_id, ollama_client=mock_ollama, memory=mem)

    # Handlers: 0=start, 1=reset, 2=plan, 3=tasks, 4=task, 5=message
    plan_handler = app.handlers[0][2].callback
    tasks_handler = app.handlers[0][3].callback
    task_handler = app.handlers[0][4].callback

    context = MagicMock()
    context.bot.send_chat_action = AsyncMock()

    # 1. Owner runs /plan Write unit tests
    update_owner = MagicMock()
    update_owner.effective_chat.type = "private"
    update_owner.effective_chat.id = owner_id
    update_owner.effective_user.id = owner_id
    update_owner.effective_message.reply_text = AsyncMock()
    update_owner.effective_message.text = "/plan Write unit tests"

    await plan_handler(update_owner, context)
    update_owner.effective_message.reply_text.assert_called_once()
    reply_text = update_owner.effective_message.reply_text.call_args[0][0]
    assert "Action Plan #1" in reply_text
    assert "Planned — not executed" in reply_text
    assert "1. Identify edge cases [Planned]" in reply_text

    # 2. Owner runs /tasks
    update_owner.effective_message.reply_text.reset_mock()
    await tasks_handler(update_owner, context)
    tasks_reply = update_owner.effective_message.reply_text.call_args[0][0]
    assert "Recent Action Plans:" in tasks_reply
    assert "#1" in tasks_reply
    assert "Planned — not executed" in tasks_reply

    # 3. Owner runs /task 1
    update_owner.effective_message.reply_text.reset_mock()
    context.args = ["1"]
    await task_handler(update_owner, context)
    detail_reply = update_owner.effective_message.reply_text.call_args[0][0]
    assert "Action Plan #1" in detail_reply
    assert "Write unit tests" in detail_reply
    assert "1. Identify edge cases [Planned]" in detail_reply

    # 4. Unauthorized user runs /plan
    update_stranger = MagicMock()
    update_stranger.effective_chat.type = "private"
    update_stranger.effective_user.id = 9999
    update_stranger.effective_message.reply_text = AsyncMock()
    update_stranger.effective_message.text = "/plan Malicious goal"

    await plan_handler(update_stranger, context)
    update_stranger.effective_message.reply_text.assert_called_with(UNAUTHORIZED_MESSAGE)
