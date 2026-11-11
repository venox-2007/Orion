"""Unit tests for Step 30 — Multi-Action Workflow & Named Macro Engine."""

import asyncio
from pathlib import Path
import re
from unittest.mock import AsyncMock, patch
import pytest

from orion.core.macro_store import MacroStore, get_macro_store
from orion.core.workflow_runner import (
    MAX_WORKFLOW_STEPS,
    execute_workflow,
    split_chained_commands,
)
from orion.tools.router import (
    check_deterministic_macro_and_workflow_query,
    route_and_execute_tool,
)


@pytest.fixture
def temp_macro_store(tmp_path: Path) -> MacroStore:
    """Create an isolated SQLite MacroStore instance for testing."""
    db_file = tmp_path / "test_macros.db"
    return MacroStore(db_path=db_file)


def test_macro_store_crud(temp_macro_store: MacroStore):
    """Verify macro save, retrieval, upsert on conflict, listing, and deletion."""
    store = temp_macro_store

    # 1. Initially empty
    assert store.list_macros() == []
    assert "No macros saved yet" in store.format_macro_list([])

    # 2. Save macro
    saved = store.save_macro("dev", ["open code", "run: git status", "briefing"], description="Dev setup")
    assert saved["name"] == "dev"
    assert saved["steps"] == ["open code", "run: git status", "briefing"]
    assert saved["description"] == "Dev setup"

    # 3. Retrieve macro
    retrieved = store.get_macro("dev")
    assert retrieved is not None
    assert retrieved["name"] == "dev"
    assert retrieved["steps"] == ["open code", "run: git status", "briefing"]

    # Case-insensitivity and punctuation tolerance
    assert store.get_macro("DEV") is not None
    assert store.get_macro(" dev ") is not None

    # 4. Overwrite on conflict (Upsert)
    updated = store.save_macro("dev", ["open code", "screenshot"])
    assert updated["steps"] == ["open code", "screenshot"]
    retrieved_updated = store.get_macro("dev")
    assert retrieved_updated is not None
    assert retrieved_updated["steps"] == ["open code", "screenshot"]

    # 5. List macros and formatting
    store.save_macro("morning", ["briefing", "battery"])
    macros = store.list_macros()
    assert len(macros) == 2
    # Alphabetical order: "dev", "morning"
    assert [m["name"] for m in macros] == ["dev", "morning"]

    formatted = store.format_macro_list(macros)
    assert "Saved ORION Macros:" in formatted
    assert "• *dev* (2 steps): open code → screenshot" in formatted
    assert "• *morning* (2 steps): briefing → battery" in formatted

    # 6. Delete macro
    assert store.delete_macro("dev") is True
    assert store.get_macro("dev") is None
    assert len(store.list_macros()) == 1
    assert store.delete_macro("non_existent") is False


def test_split_chained_commands():
    """Verify splitting commands across 'then', '&&', and ';' delimiters with step limits."""
    # 1. Delimiter: ' then '
    s1 = split_chained_commands("open notepad then type hello then screenshot")
    assert s1 == ["open notepad", "type hello", "screenshot"]

    # 2. Delimiter: '&&'
    s2 = split_chained_commands("open calc && type 42")
    assert s2 == ["open calc", "type 42"]

    # 3. Delimiter: ';'
    s3 = split_chained_commands("battery ; cpu ; storage")
    assert s3 == ["battery", "cpu", "storage"]

    # 4. Mixed delimiters
    s4 = split_chained_commands("open code then run: git status && briefing")
    assert s4 == ["open code", "run: git status", "briefing"]

    # 5. Single command (no chain)
    s5 = split_chained_commands("screenshot")
    assert s5 == ["screenshot"]

    # 6. Empty command
    assert split_chained_commands("") == []
    assert split_chained_commands("   ") == []

    # 7. Max workflow steps cap (max 6)
    long_chain = "s1 then s2 then s3 then s4 then s5 then s6 then s7 then s8"
    capped = split_chained_commands(long_chain)
    assert len(capped) == MAX_WORKFLOW_STEPS
    assert capped == ["s1", "s2", "s3", "s4", "s5", "s6"]


@pytest.mark.asyncio
async def test_workflow_execution_mocked():
    """Verify sequential workflow execution, delay pacing, failure abort, and Markdown formatting."""
    call_log = []

    async def mock_router(cmd: str, context=None):
        call_log.append(cmd)
        if cmd == "fail_step":
            return {"handled": True, "reply": "⚠️ Failed to execute action."}
        return {"handled": True, "reply": f"Output for {cmd}"}

    # 1. Successful 3-step workflow
    steps = ["open notepad", "type hello", "screenshot"]
    res = await execute_workflow(steps, mock_router, delay_sec=0.01)
    assert res["handled"] is True
    assert res["is_workflow"] is True
    assert res["success"] is True
    assert res["completed"] == 3
    assert res["total"] == 3
    assert call_log == ["open notepad", "type hello", "screenshot"]
    assert "⚡ *Workflow Completed (3/3 steps):*" in res["reply"]
    assert "1. ✅ *open notepad*" in res["reply"]
    assert "2. ✅ *type hello*" in res["reply"]
    assert "3. ✅ *screenshot*" in res["reply"]
    assert re.search(r"</?(?:b|i|s|code|pre)>", res["reply"]) is None

    # 2. Workflow failure aborts subsequent steps
    call_log.clear()
    fail_steps = ["open notepad", "fail_step", "screenshot"]
    res_fail = await execute_workflow(fail_steps, mock_router, delay_sec=0.01)
    assert res_fail["handled"] is True
    assert res_fail["success"] is False
    assert res_fail["completed"] == 1
    assert call_log == ["open notepad", "fail_step"]  # "screenshot" was aborted
    assert "⚡ *Workflow Stopped (1/3 steps):*" in res_fail["reply"]
    assert "1. ✅ *open notepad*" in res_fail["reply"]
    assert "2. ❌ *fail_step*" in res_fail["reply"]


@pytest.mark.asyncio
async def test_router_macro_queries(temp_macro_store: MacroStore):
    """Verify saving macros, listing macros, deleting macros, and executing macros via router."""
    with patch("orion.core.macro_store.get_macro_store", return_value=temp_macro_store):
        # 1. Save macro
        save_query = "macro save test: open notepad then type hello"
        res_save = await route_and_execute_tool(save_query)
        assert res_save is not None
        assert res_save.get("handled") is True
        assert "⚡ *Macro 'test' saved* with 2 steps." in res_save.get("reply", "")
        assert re.search(r"</?(?:b|i|s|code|pre)>", res_save["reply"]) is None

        # Verify saved in store
        macro = temp_macro_store.get_macro("test")
        assert macro is not None
        assert macro["steps"] == ["open notepad", "type hello"]

        # 2. List macros
        res_list = await route_and_execute_tool("list macros")
        assert res_list is not None
        assert res_list.get("handled") is True
        assert "Saved ORION Macros:" in res_list.get("reply", "")
        assert "• *test* (2 steps): open notepad → type hello" in res_list.get("reply", "")
        assert re.search(r"</?(?:b|i|s|code|pre)>", res_list["reply"]) is None

        # 3. Execute named macro via router
        with patch("orion.core.workflow_runner.execute_workflow") as mock_exec:
            mock_exec.return_value = {
                "handled": True,
                "is_workflow": True,
                "success": True,
                "completed": 2,
                "total": 2,
                "reply": "⚡ *Workflow Completed (2/2 steps):*\n1. ✅ *open notepad*\n2. ✅ *type hello*",
            }

            res_run = await route_and_execute_tool("macro test")
            assert res_run is not None
            assert res_run.get("handled") is True
            mock_exec.assert_called_once()
            args = mock_exec.call_args[0]
            assert args[0] == ["open notepad", "type hello"]

        # 4. Delete macro
        res_del = await route_and_execute_tool("delete macro test")
        assert res_del is not None
        assert res_del.get("handled") is True
        assert "🗑️ *Macro 'test' deleted.*" in res_del.get("reply", "")
        assert temp_macro_store.get_macro("test") is None

        # 5. Run non-existent macro
        res_missing = await route_and_execute_tool("macro unknown")
        assert res_missing is not None
        assert res_missing.get("handled") is True
        assert "⚠️ Macro '*unknown*' not found." in res_missing.get("reply", "")


@pytest.mark.asyncio
async def test_router_adhoc_workflow_chaining():
    """Verify ad-hoc command chaining triggers workflow_runner.execute_workflow."""
    with patch("orion.core.workflow_runner.execute_workflow") as mock_exec:
        mock_exec.return_value = {
            "handled": True,
            "is_workflow": True,
            "success": True,
            "completed": 3,
            "total": 3,
            "reply": "⚡ *Workflow Completed (3/3 steps):*\n1. ✅ *open notepad*\n2. ✅ *type hello*\n3. ✅ *screenshot*",
        }

        res = await route_and_execute_tool("open notepad then type hello then screenshot")
        assert res is not None
        assert res.get("handled") is True
        mock_exec.assert_called_once()
        args = mock_exec.call_args[0]
        assert args[0] == ["open notepad", "type hello", "screenshot"]
        assert "Workflow Completed" in res["reply"]
        assert re.search(r"</?(?:b|i|s|code|pre)>", res["reply"]) is None
