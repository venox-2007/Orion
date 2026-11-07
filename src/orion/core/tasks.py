"""Personal task management (To-Do) engine for ORION (Step 19)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from orion.memory.database import ConversationMemory


def _get_memory(
    memory: ConversationMemory | None = None,
    db_path: str | None = None,
) -> ConversationMemory:
    """Helper to return an active ConversationMemory instance."""
    if memory is not None:
        return memory
    if db_path is not None:
        return ConversationMemory(db_path=db_path)
    return ConversationMemory()


def add_task(
    title: str,
    priority: str = "normal",
    memory: ConversationMemory | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Add a new task to persistent storage."""
    clean_title = title.strip()
    if not clean_title:
        raise ValueError("Task title cannot be empty.")

    prio = priority.strip().lower()
    if prio == "medium":
        prio = "normal"
    if prio not in {"low", "normal", "high"}:
        prio = "normal"

    mem = _get_memory(memory, db_path)
    return mem.add_task(title=clean_title, priority=prio)


def list_tasks(
    include_completed: bool = False,
    memory: ConversationMemory | None = None,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    """Query tasks ordered by status (pending first), priority, and creation order."""
    mem = _get_memory(memory, db_path)
    return mem.list_tasks(include_completed=include_completed)


def complete_task(
    task_id: int,
    memory: ConversationMemory | None = None,
    db_path: str | None = None,
) -> dict[str, Any] | None:
    """Mark a task as completed."""
    mem = _get_memory(memory, db_path)
    return mem.complete_task(task_id=task_id)


def delete_task(
    task_id: int,
    memory: ConversationMemory | None = None,
    db_path: str | None = None,
) -> bool:
    """Delete a task by its ID."""
    mem = _get_memory(memory, db_path)
    return mem.delete_task(task_id=task_id)


def format_task_list(
    tasks: list[dict[str, Any]],
    show_completed: bool = False,
) -> str:
    """Format task list for human-friendly Telegram output."""
    if not tasks:
        if show_completed:
            return "Your task list is empty."
        return "You have no pending tasks! Great job."

    lines = ["📋 <b>Your Tasks:</b>\n"]
    for t in tasks:
        tid = t["id"]
        title = t["title"]
        prio = t.get("priority", "normal").lower()
        is_done = t.get("status") == "completed"

        if is_done:
            lines.append(f"• <s>{title}</s> (Completed, ID: {tid})")
        elif prio == "high":
            lines.append(f"• [HIGH] <b>{title}</b> (ID: {tid})")
        elif prio == "low":
            lines.append(f"• [Low] {title} (ID: {tid})")
        else:
            lines.append(f"• {title} (ID: {tid})")

    lines.append("\n<i>Use 'complete task <id>' to mark a task as done.</i>")
    return "\n".join(lines)
