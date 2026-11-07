"""Unit tests for Step 19: Personal Task Management System."""

import pytest

from orion.core.tasks import add_task, complete_task, delete_task, format_task_list, list_tasks
from orion.memory.database import ConversationMemory
from orion.tools.router import check_deterministic_task_query, route_and_execute_tool


@pytest.fixture
def temp_memory(tmp_path):
    """Fixture providing a fresh ConversationMemory instance backed by a temporary SQLite DB."""
    db_file = str(tmp_path / "test_tasks.db")
    return ConversationMemory(db_path=db_file)


def test_add_task_default_and_priorities(temp_memory):
    """Adding task with default priority, explicit priorities, and validation of empty title."""
    # 1. Default priority -> normal
    t1 = add_task("Default task", memory=temp_memory)
    assert t1["id"] == 1
    assert t1["title"] == "Default task"
    assert t1["priority"] == "normal"
    assert t1["status"] == "pending"
    assert t1["created_at"] is not None
    assert t1["completed_at"] is None

    # 2. Explicit 'high'
    t2 = add_task("High priority item", priority="high", memory=temp_memory)
    assert t2["id"] == 2
    assert t2["priority"] == "high"

    # 3. Explicit 'low'
    t3 = add_task("Low priority item", priority="low", memory=temp_memory)
    assert t3["id"] == 3
    assert t3["priority"] == "low"

    # 4. Explicit 'medium' -> mapped to 'normal'
    t4 = add_task("Medium priority item", priority="medium", memory=temp_memory)
    assert t4["id"] == 4
    assert t4["priority"] == "normal"

    # 5. Empty title raises ValueError
    with pytest.raises(ValueError, match="Task title cannot be empty"):
        add_task("   ", memory=temp_memory)


def test_list_tasks_ordering(temp_memory):
    """Insert tasks with random priorities; list_tasks orders by high, normal, then low."""
    add_task("Normal task 1", priority="normal", memory=temp_memory)
    add_task("Low task 1", priority="low", memory=temp_memory)
    add_task("High task 1", priority="high", memory=temp_memory)
    add_task("High task 2", priority="high", memory=temp_memory)
    add_task("Low task 2", priority="low", memory=temp_memory)
    add_task("Normal task 2", priority="normal", memory=temp_memory)

    pending_tasks = list_tasks(include_completed=False, memory=temp_memory)
    assert len(pending_tasks) == 6

    priorities = [t["priority"] for t in pending_tasks]
    assert priorities == ["high", "high", "normal", "normal", "low", "low"]

    # Verify tie-breaker is creation order (id ASC)
    high_ids = [t["id"] for t in pending_tasks if t["priority"] == "high"]
    assert high_ids == sorted(high_ids)


def test_complete_task_lifecycle(temp_memory):
    """Add task, verify pending state, complete task, verify completed state, and handle non-existent ID."""
    t = add_task("Work on report", priority="high", memory=temp_memory)
    assert t["status"] == "pending"
    assert t["completed_at"] is None

    # Complete task
    completed = complete_task(t["id"], memory=temp_memory)
    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["completed_at"] is not None

    # Completing an already completed task returns existing record
    again = complete_task(t["id"], memory=temp_memory)
    assert again is not None
    assert again["status"] == "completed"

    # Complete non-existent ID returns None
    assert complete_task(999, memory=temp_memory) is None


def test_delete_task(temp_memory):
    """Delete existing task removes row and returns True; non-existent ID returns False."""
    t = add_task("Temporary task", memory=temp_memory)
    assert delete_task(t["id"], memory=temp_memory) is True

    # Row is gone
    assert complete_task(t["id"], memory=temp_memory) is None
    assert delete_task(t["id"], memory=temp_memory) is False

    # Non-existent ID returns False
    assert delete_task(9999, memory=temp_memory) is False


def test_task_formatting():
    """Empty list returns friendly zero-task message; populated list shows priority tags and IDs."""
    # 1. Empty pending list
    empty_pending = format_task_list([], show_completed=False)
    assert "You have no pending tasks! Great job." in empty_pending

    # 2. Empty all list
    empty_all = format_task_list([], show_completed=True)
    assert "Your task list is empty." in empty_all

    # 3. Populated list
    sample_tasks = [
        {"id": 1, "title": "Deploy hotfix", "priority": "high", "status": "pending"},
        {"id": 2, "title": "Buy milk", "priority": "normal", "status": "pending"},
        {"id": 3, "title": "Read article", "priority": "low", "status": "pending"},
        {"id": 4, "title": "Call doctor", "priority": "normal", "status": "completed"},
    ]
    formatted = format_task_list(sample_tasks, show_completed=True)
    assert "📋 <b>Your Tasks:</b>" in formatted
    assert "• [HIGH] <b>Deploy hotfix</b> (ID: 1)" in formatted
    assert "• Buy milk (ID: 2)" in formatted
    assert "• [Low] Read article (ID: 3)" in formatted
    assert "• <s>Call doctor</s> (Completed, ID: 4)" in formatted
    assert "Use 'complete task <id>' to mark a task as done." in formatted


@pytest.mark.asyncio
async def test_deterministic_router_task_queries(temp_memory):
    """Test deterministic router for add, list, complete, delete, and reminder non-interception."""
    ctx = {"memory": temp_memory, "user_id": 1234, "owner_id": 1234}

    # 1. Add task high priority
    res1 = await route_and_execute_tool("add task high priority: Finish assignment", context=ctx)
    assert res1 is not None
    assert res1.get("handled") is True
    assert "✅ Added task: <b>Finish assignment</b> (Priority: high, ID: 1)" in res1["reply"]

    # 2. Add task to buy groceries (normal priority)
    res2 = await route_and_execute_tool("add a task to buy groceries", context=ctx)
    assert res2 is not None
    assert res2.get("handled") is True
    assert "✅ Added task: <b>buy groceries</b> (Priority: normal, ID: 2)" in res2["reply"]

    # 3. Show my tasks
    res_list = await route_and_execute_tool("show my tasks", context=ctx)
    assert res_list is not None
    assert res_list.get("handled") is True
    assert "📋 <b>Your Tasks:</b>" in res_list["reply"]
    assert "• [HIGH] <b>Finish assignment</b> (ID: 1)" in res_list["reply"]
    assert "• buy groceries (ID: 2)" in res_list["reply"]

    # 4. Complete task 1
    res_comp = await route_and_execute_tool("complete task 1", context=ctx)
    assert res_comp is not None
    assert res_comp.get("handled") is True
    assert "🎉 Marked task #1 (<b>Finish assignment</b>) as completed!" in res_comp["reply"]

    # 5. Delete task 1
    res_del = await route_and_execute_tool("delete task 1", context=ctx)
    assert res_del is not None
    assert res_del.get("handled") is True
    assert "🗑️ Deleted task #1." in res_del["reply"]

    # 6. Delete already deleted task returns not found
    res_del_again = await route_and_execute_tool("delete task 1", context=ctx)
    assert res_del_again is not None
    assert res_del_again.get("handled") is True
    assert "Task #1 was not found." in res_del_again["reply"]

    # 7. 'remind me in 10 minutes to take a break' is NOT intercepted by task router
    assert check_deterministic_task_query("remind me in 10 minutes to take a break", context=ctx) is None
