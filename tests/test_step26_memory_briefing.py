"""Unit tests for Step 26 — Unified Long-Term Memory Store & Executive Briefing Engine."""

import re
import time
from unittest.mock import MagicMock, patch
import pytest

from orion.core.briefing import generate_executive_briefing
from orion.core.memory_store import MemoryStore
from orion.core.reminders import ReminderManager
from orion.core.tasks import TaskManager
from orion.memory.database import ConversationMemory
from orion.tools.router import (
    check_deterministic_memory_and_briefing_query,
    route_and_execute_tool,
)


@pytest.fixture
def temp_db(tmp_path):
    """Create a temporary database path for isolated tests."""
    return tmp_path / "test_orion.db"


def test_memory_crud_and_conflict(temp_db):
    """Verify saving facts, slug generation, searching, listing, and upsert conflict resolution."""
    store = MemoryStore(db_path=temp_db)

    # 1. Save fact with auto-generated slug
    rec1 = store.save_fact("FastAPI runs on port 8080", category="tech")
    assert rec1["id"] == 1
    assert rec1["key"] == "fastapi_runs_on_port"
    assert rec1["fact"] == "FastAPI runs on port 8080"
    assert rec1["category"] == "tech"

    # 2. Save fact with explicit key
    rec2 = store.save_fact("Gym session at 6 PM daily", key="workout", category="schedule")
    assert rec2["key"] == "workout"
    assert rec2["category"] == "schedule"

    # 3. List all memories
    all_mems = store.get_all_memories()
    assert len(all_mems) == 2
    assert all_mems[0]["key"] == "workout"  # Ordered by updated_at DESC
    assert all_mems[1]["key"] == "fastapi_runs_on_port"

    # 4. Search memories (LIKE query on key and fact)
    search_res = store.find_memories("8080")
    assert len(search_res) == 1
    assert search_res[0]["key"] == "fastapi_runs_on_port"

    search_key = store.find_memories("workout")
    assert len(search_key) == 1
    assert search_key[0]["fact"] == "Gym session at 6 PM daily"

    # 5. Conflict update on existing key
    rec2_updated = store.save_fact("Gym session at 7 PM on weekends", key="workout", category="schedule")
    assert rec2_updated["id"] == rec2["id"]
    assert rec2_updated["fact"] == "Gym session at 7 PM on weekends"

    all_mems_after = store.get_all_memories()
    assert len(all_mems_after) == 2
    assert all_mems_after[0]["fact"] == "Gym session at 7 PM on weekends"

    # 6. Format memories list
    formatted = store.format_memories_list(all_mems_after)
    assert "🧠 *ORION Stored Memories:*" in formatted
    assert "• [#2] *workout*: Gym session at 7 PM on weekends" in formatted
    assert re.search(r"</?(?:b|i|s|code|pre)>", formatted) is None

    # 7. Prompt injection context length guard (<= 500 chars)
    ctx = store.get_prompt_injection_context(limit=10)
    assert len(ctx) <= 500
    assert "workout: Gym session at 7 PM on weekends" in ctx


def test_memory_delete(temp_db):
    """Verify deleting memory by numeric ID and by slug key."""
    store = MemoryStore(db_path=temp_db)

    rec1 = store.save_fact("My name is Alice", key="user_name", category="identity")
    rec2 = store.save_fact("Dark mode preferred", key="theme_pref", category="preference")
    assert len(store.get_all_memories()) == 2

    # 1. Delete by numeric ID
    del_id_success = store.delete_memory(str(rec1["id"]))
    assert del_id_success is True
    assert len(store.get_all_memories()) == 1

    # Deleting non-existent ID returns False
    assert store.delete_memory("9999") is False

    # 2. Delete by slug key
    del_key_success = store.delete_memory("theme_pref")
    assert del_key_success is True
    assert len(store.get_all_memories()) == 0

    # Deleting non-existent key returns False
    assert store.delete_memory("non_existent_key") is False

    # Empty list formatting
    empty_fmt = store.format_memories_list([])
    assert "No memories stored yet" in empty_fmt


def test_executive_briefing_aggregation(temp_db):
    """Verify executive briefing aggregates vitals, tasks, reminders, and memories."""
    # Setup database with tasks, reminders, and memories
    mem = ConversationMemory(db_path=temp_db)
    mem.add_task("Finish documentation", priority="high")
    mem.add_task("Submit expense report", priority="normal")

    # Add reminder within next 24 hours
    now_ts = time.time()
    mem.create_reminder(
        user_id=12345,
        message="Standup meeting",
        scheduled_at=now_ts + 3600,
        recurrence=None,
        timezone="UTC",
    )

    store = MemoryStore(db_path=temp_db)
    store.save_fact("Server runs on port 9000", key="server_port")

    # Mock psutil
    mock_battery = MagicMock()
    mock_battery.percent = 85
    mock_battery.power_plugged = True

    mock_ram = MagicMock()
    mock_ram.used = 8 * 1024**3
    mock_ram.total = 16 * 1024**3

    with patch("psutil.sensors_battery", return_value=mock_battery), \
         patch("psutil.cpu_percent", return_value=15.0), \
         patch("psutil.virtual_memory", return_value=mock_ram):

        briefing = generate_executive_briefing(db_path=temp_db)

        # 1. Header & Date
        assert "🌅 *ORION Executive Briefing —" in briefing

        # 2. Vitals
        assert "📊 *System Vitals:*" in briefing
        assert "⚡ 85% (Charging)" in briefing
        assert "CPU: 15% | RAM: 8.0/16.0 GB" in briefing

        # 3. Tasks
        assert "📋 *Pending Tasks (2):*" in briefing
        assert "• Finish documentation" in briefing
        assert "• Submit expense report" in briefing

        # 4. Reminders
        assert "⏰ *Upcoming Reminders (24h):*" in briefing
        assert "Standup meeting" in briefing

        # 5. Memories
        assert "🧠 *Pinned Memories:*" in briefing
        assert "*server_port*: Server runs on port 9000" in briefing

        # Pure Telegram Markdown check (zero raw HTML tags)
        assert re.search(r"</?(?:b|i|s|code|pre)>", briefing) is None


@pytest.mark.asyncio
async def test_router_memory_and_briefing(temp_db):
    """Verify router matches daily briefing, remembering facts, listing, and forgetting memories."""
    ctx = {"db_path": str(temp_db)}

    # 1. Remember fact
    res_rem = await route_and_execute_tool("remember that my server port is 8080", context=ctx)
    assert res_rem is not None
    assert res_rem.get("handled") is True
    assert "🧠 *Memory stored:*" in res_rem.get("reply", "")
    assert "my server port is 8080" in res_rem.get("reply", "")
    assert re.search(r"</?(?:b|i|s|code|pre)>", res_rem["reply"]) is None

    # Alternative pattern: "store memory: Python 3.11 is default"
    res_store = await route_and_execute_tool("store memory: Python 3.11 is default", context=ctx)
    assert res_store is not None
    assert res_store.get("handled") is True
    assert "Python 3.11 is default" in res_store.get("reply", "")

    # 2. Show memories
    res_list = await route_and_execute_tool("show memories", context=ctx)
    assert res_list is not None
    assert res_list.get("handled") is True
    assert "🧠 *ORION Stored Memories:*" in res_list.get("reply", "")
    assert "my server port is 8080" in res_list.get("reply", "")
    assert "Python 3.11 is default" in res_list.get("reply", "")
    assert re.search(r"</?(?:b|i|s|code|pre)>", res_list["reply"]) is None

    # Alternative phrase: "what do you remember about me"
    res_remember = await route_and_execute_tool("what do you remember about me?", context=ctx)
    assert res_remember is not None and res_remember.get("handled") is True
    assert "🧠 *ORION Stored Memories:*" in res_remember.get("reply", "")

    # 3. Daily briefing
    with patch("psutil.sensors_battery", return_value=None), \
         patch("psutil.cpu_percent", return_value=5.0), \
         patch("psutil.virtual_memory", return_value=MagicMock(used=4*1024**3, total=16*1024**3)):
        res_briefing = await route_and_execute_tool("morning briefing", context=ctx)
        assert res_briefing is not None
        assert res_briefing.get("handled") is True
        assert "🌅 *ORION Executive Briefing —" in res_briefing.get("reply", "")
        assert "📊 *System Vitals:*" in res_briefing.get("reply", "")
        assert re.search(r"</?(?:b|i|s|code|pre)>", res_briefing["reply"]) is None

        # Alternative briefing queries
        res_briefing2 = await route_and_execute_tool("briefing", context=ctx)
        assert res_briefing2 is not None and res_briefing2.get("handled") is True
        assert "🌅 *ORION Executive Briefing —" in res_briefing2.get("reply", "")

        res_briefing3 = await route_and_execute_tool("give me my daily briefing", context=ctx)
        assert res_briefing3 is not None and res_briefing3.get("handled") is True

    # 4. Forget memory
    # First check what keys we have
    store = MemoryStore(db_path=temp_db)
    mems = store.get_all_memories()
    target_key = mems[0]["key"]

    res_forget = await route_and_execute_tool(f"forget memory {target_key}", context=ctx)
    assert res_forget is not None
    assert res_forget.get("handled") is True
    assert f"🗑️ *Memory forgotten:* `{target_key}`" in res_forget.get("reply", "")

    # Forgetting non-existent memory
    res_forget_missing = await route_and_execute_tool("forget memory non_existent_key_xyz", context=ctx)
    assert res_forget_missing is not None
    assert res_forget_missing.get("handled") is True
    assert "⚠️ Memory `non_existent_key_xyz` not found." in res_forget_missing.get("reply", "")


@pytest.mark.asyncio
async def test_telegram_bot_prompt_injection(temp_db):
    """Verify that stored memories are injected into Ollama system prompt on conversational fall-through."""
    from unittest.mock import AsyncMock
    from orion.integrations.telegram_bot import create_telegram_app

    store = MemoryStore(db_path=temp_db)
    store.save_fact("User loves TypeScript and Rust", key="fav_languages", category="preference")

    mem = ConversationMemory(db_path=temp_db)
    owner_id = 12345
    token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"

    mock_ollama = MagicMock()
    mock_ollama.generate_response = AsyncMock(return_value="That's awesome!")
    mock_ollama.generate_structured_json = AsyncMock(return_value=None)

    app = create_telegram_app(token=token, owner_id=owner_id, ollama_client=mock_ollama, memory=mem)
    message_handler_func = app.handlers[0][-1].callback

    context = MagicMock()
    context.bot.send_chat_action = AsyncMock()

    update_owner = MagicMock()
    update_owner.effective_chat.type = "private"
    update_owner.effective_chat.id = owner_id
    update_owner.effective_user.id = owner_id
    update_owner.effective_message.reply_text = AsyncMock()
    update_owner.effective_message.text = "Hello ORION, what should we build today?"

    await message_handler_func(update_owner, context)

    # Check mock_ollama.generate_response call arguments
    mock_ollama.generate_response.assert_called_once()
    call_args = mock_ollama.generate_response.call_args
    system_prompt_arg = call_args.kwargs.get("system_prompt", "")
    assert "Known facts about user:" in system_prompt_arg
    assert "fav_languages: User loves TypeScript and Rust" in system_prompt_arg
