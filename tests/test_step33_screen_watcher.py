"""Tests for Step 33 — Conditional Screen Watcher & Workflow Wait Triggers."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from orion.core.workflow_runner import execute_workflow
from orion.tools import screen_watcher
from orion.tools.router import route_and_execute_tool


@pytest.fixture(autouse=True)
def clean_watchers():
    """Ensure screen watcher registry is clear before and after each test."""
    screen_watcher.cancel_watcher("all")
    screen_watcher._ACTIVE_WATCHERS.clear()
    screen_watcher._WATCHER_META.clear()
    screen_watcher._WATCHER_COUNTER = 0
    yield
    screen_watcher.cancel_watcher("all")
    screen_watcher._ACTIVE_WATCHERS.clear()
    screen_watcher._WATCHER_META.clear()


@pytest.mark.asyncio
async def test_wait_for_screen_text_success():
    """Test wait_for_screen_text succeeds when query is detected."""
    call_count = 0

    async def mock_find(query: str, target_app: str | None = None):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            return 250, 400
        return None

    with patch("orion.tools.screen_ocr.find_text_coordinates", side_effect=mock_find):
        success, msg = await screen_watcher.wait_for_screen_text(
            "Ready",
            timeout_sec=5.0,
            poll_interval=0.01,
        )

    assert success is True
    assert "👁️ Detected *'Ready'* on screen at (250, 400)." in msg
    assert "<" not in msg and ">" not in msg


@pytest.mark.asyncio
async def test_wait_for_screen_text_timeout():
    """Test wait_for_screen_text times out when text is not detected."""
    with patch("orion.tools.screen_ocr.find_text_coordinates", new=AsyncMock(return_value=None)):
        success, msg = await screen_watcher.wait_for_screen_text(
            "NonExistent",
            timeout_sec=0.1,
            poll_interval=0.02,
        )

    assert success is False
    assert msg.startswith("⚠️ Timed out after")
    assert "*'NonExistent'*" in msg
    assert "<" not in msg and ">" not in msg


@pytest.mark.asyncio
async def test_start_background_watcher_notification():
    """Test start_background_watcher dispatches alert callback and cleans up registry."""
    notified = []

    async def fake_notify(msg: str):
        notified.append(msg)

    async def mock_find(query: str, target_app: str | None = None):
        return 120, 340

    with patch("orion.tools.screen_ocr.find_text_coordinates", side_effect=mock_find):
        watch_id = screen_watcher.start_background_watcher(
            "Build Succeeded",
            fake_notify,
            timeout_sec=5.0,
            poll_interval=0.01,
        )

        assert watch_id.startswith("watch_")
        assert watch_id in screen_watcher._ACTIVE_WATCHERS

        # Wait for background task to complete
        task = screen_watcher._ACTIVE_WATCHERS[watch_id]
        await asyncio.wait_for(task, timeout=2.0)

    assert len(notified) == 1
    assert "🔔 *Screen Alert:* Detected *'Build Succeeded'* at (120, 340)!" in notified[0]
    assert "<" not in notified[0] and ">" not in notified[0]
    assert watch_id not in screen_watcher._ACTIVE_WATCHERS


@pytest.mark.asyncio
async def test_cancel_watcher():
    """Test cancel_watcher terminates active task and removes it from registry."""
    async def mock_find(query: str, target_app: str | None = None):
        return None

    with patch("orion.tools.screen_ocr.find_text_coordinates", side_effect=mock_find):
        watch_id = screen_watcher.start_background_watcher(
            "NeverAppears",
            None,
            timeout_sec=60.0,
            poll_interval=0.1,
        )

        watchers = screen_watcher.list_watchers()
        assert len(watchers) == 1
        assert watchers[0]["id"] == watch_id
        assert watchers[0]["query"] == "NeverAppears"

        cancelled = screen_watcher.cancel_watcher(watch_id)
        assert cancelled == 1
        assert len(screen_watcher.list_watchers()) == 0


@pytest.mark.asyncio
async def test_workflow_wait_step_success():
    """Test wait for text in a workflow allows subsequent steps to proceed on match."""
    async def mock_router(step_cmd: str, context: dict[str, Any] | None = None):
        if "wait for" in step_cmd:
            return {"handled": True, "reply": "👁️ Detected *'Loaded'* on screen at (100, 200)."}
        return {"handled": True, "reply": f"Executed {step_cmd}"}

    steps = ["ps: Get-Process", "wait for 'Loaded'", "where is my mouse"]
    res = await execute_workflow(steps, mock_router, delay_sec=0.01)

    assert res["success"] is True
    assert res["completed"] == 3
    assert "⚡ *Workflow Completed (3/3 steps):*" in res["reply"]
    assert "👁️ Detected *'Loaded'*" in res["reply"]


@pytest.mark.asyncio
async def test_workflow_wait_step_timeout_halts():
    """Test wait for text timing out in a workflow halts remaining steps."""
    async def mock_router(step_cmd: str, context: dict[str, Any] | None = None):
        if "wait for" in step_cmd:
            return {"handled": True, "reply": "⚠️ Timed out after 30s waiting for *'Missing'*."}
        return {"handled": True, "reply": f"Executed {step_cmd}"}

    steps = ["ps: Get-Process", "wait for 'Missing'", "where is my mouse"]
    res = await execute_workflow(steps, mock_router, delay_sec=0.01)

    assert res["success"] is False
    assert res["completed"] == 1
    assert "⚡ *Workflow Stopped (1/3 steps):*" in res["reply"]
    assert "❌ *wait for 'Missing'*" in res["reply"]


@pytest.mark.asyncio
async def test_router_watcher_commands():
    """Test deterministic router handles wait, watch, list watchers, and cancel watchers."""
    # 1. Synchronous wait command
    with patch("orion.tools.screen_watcher.wait_for_screen_text", new=AsyncMock(return_value=(True, "👁️ Detected *'Done'* on screen at (50, 60)."))) as mock_wait:
        res1 = await route_and_execute_tool('wait for "Done" in chrome timeout 10s')
        assert res1 is not None
        assert res1["handled"] is True
        assert "👁️ Detected *'Done'* on screen at (50, 60)." in res1["reply"]
        mock_wait.assert_called_once_with("Done", target_app="chrome", timeout_sec=10.0)

    # 2. Asynchronous watch command
    res2 = await route_and_execute_tool('watch for "Build Succeeded"')
    assert res2 is not None
    assert res2["handled"] is True
    assert "👁️ *Watching screen for 'Build Succeeded'*" in res2["reply"]
    assert "(ID: `watch_1`, timeout: 15m)" in res2["reply"]

    # 3. List watchers
    res3 = await route_and_execute_tool("list watchers")
    assert res3 is not None
    assert res3["handled"] is True
    assert "👁️ *Active Screen Watchers (1):*" in res3["reply"]
    assert "• `watch_1`: watching for *'Build Succeeded'*" in res3["reply"]

    # 4. Stop watchers
    res4 = await route_and_execute_tool("stop watchers")
    assert res4 is not None
    assert res4["handled"] is True
    assert "🛑 Cancelled 1 active screen watcher(s)." in res4["reply"]

    # 5. List watchers when empty
    res5 = await route_and_execute_tool("watchers")
    assert res5 is not None
    assert res5["handled"] is True
    assert "_No background screen watchers currently active._" in res5["reply"]
