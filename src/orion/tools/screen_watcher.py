"""Conditional Screen Watcher and Workflow Wait Triggers for ORION (Step 33).

Provides synchronous waiting for on-screen text during workflow steps and
background asynchronous screen monitoring with proactive Telegram notifications.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from orion.tools import screen_ocr

logger = logging.getLogger("orion.tools.screen_watcher")

_ACTIVE_WATCHERS: dict[str, asyncio.Task] = {}
_WATCHER_META: dict[str, dict[str, Any]] = {}
_WATCHER_COUNTER: int = 0


async def wait_for_screen_text(
    query: str,
    target_app: str | None = None,
    timeout_sec: float = 30.0,
    poll_interval: float = 1.5,
) -> tuple[bool, str]:
    """Synchronously poll screen OCR until query text appears or timeout_sec expires."""
    clean_query = query.strip()
    if not clean_query:
        return False, "⚠️ No search text provided to wait for."

    timeout = max(0.1, float(timeout_sec))
    poll = max(0.1, float(poll_interval))
    start_time = asyncio.get_running_loop().time()
    deadline = start_time + timeout

    while True:
        try:
            coords = await screen_ocr.find_text_coordinates(clean_query, target_app=target_app)
            if coords is not None:
                x, y = coords
                return True, f"👁️ Detected *'{clean_query}'* on screen at ({x}, {y})."
        except Exception as ocr_err:
            logger.debug("OCR error while polling for '%s': %s", clean_query, ocr_err)

        now = asyncio.get_running_loop().time()
        if now >= deadline:
            break

        sleep_time = min(poll, max(0.01, deadline - now))
        await asyncio.sleep(sleep_time)

    return False, f"⚠️ Timed out after {int(timeout)}s waiting for *'{clean_query}'*."


def start_background_watcher(
    query: str,
    notify_cb: Callable[[str], Any] | None,
    target_app: str | None = None,
    timeout_sec: float = 900.0,
    poll_interval: float = 3.0,
) -> str:
    """Spawn a background asyncio.Task polling screen OCR for query text."""
    global _WATCHER_COUNTER
    clean_query = query.strip()
    _WATCHER_COUNTER += 1
    watch_id = f"watch_{_WATCHER_COUNTER}"

    timeout = max(1.0, float(timeout_sec))
    poll = max(0.1, float(poll_interval))

    async def _watcher_loop() -> None:
        start_time = asyncio.get_running_loop().time()
        deadline = start_time + timeout
        try:
            while True:
                now = asyncio.get_running_loop().time()
                if now >= deadline:
                    mins = int(timeout // 60)
                    time_str = f"{mins}m" if mins >= 1 else f"{int(timeout)}s"
                    timeout_msg = f"⚠️ *Screen Watcher Expired:* Timed out after {time_str} watching for *'{clean_query}'*."
                    if notify_cb:
                        try:
                            res = notify_cb(timeout_msg)
                            if asyncio.iscoroutine(res):
                                await res
                        except Exception as n_err:
                            logger.error("Watcher notify callback failed on timeout: %s", n_err)
                    break

                try:
                    coords = await screen_ocr.find_text_coordinates(clean_query, target_app=target_app)
                except Exception as ocr_err:
                    logger.debug("Background watcher OCR poll error: %s", ocr_err)
                    coords = None

                if coords is not None:
                    x, y = coords
                    alert_msg = f"🔔 *Screen Alert:* Detected *'{clean_query}'* at ({x}, {y})!"
                    if notify_cb:
                        try:
                            res = notify_cb(alert_msg)
                            if asyncio.iscoroutine(res):
                                await res
                        except Exception as n_err:
                            logger.error("Watcher notify callback failed: %s", n_err)
                    break

                sleep_time = min(poll, max(0.01, deadline - asyncio.get_running_loop().time()))
                await asyncio.sleep(sleep_time)

        except asyncio.CancelledError:
            logger.debug("Screen watcher %s cancelled.", watch_id)
            raise
        finally:
            _ACTIVE_WATCHERS.pop(watch_id, None)
            _WATCHER_META.pop(watch_id, None)

    task = asyncio.create_task(_watcher_loop())
    _ACTIVE_WATCHERS[watch_id] = task
    _WATCHER_META[watch_id] = {
        "id": watch_id,
        "query": clean_query,
        "target_app": target_app,
        "timeout_sec": timeout,
        "created_at": time.time(),
    }
    return watch_id


def list_watchers() -> list[dict[str, Any]]:
    """List all currently active screen watchers."""
    dead = [wid for wid, t in _ACTIVE_WATCHERS.items() if t.done()]
    for wid in dead:
        _ACTIVE_WATCHERS.pop(wid, None)
        _WATCHER_META.pop(wid, None)

    return [dict(meta) for meta in _WATCHER_META.values()]


def cancel_watcher(watch_id: str | None = None) -> int:
    """Cancel one or all active screen watchers."""
    dead = [wid for wid, t in _ACTIVE_WATCHERS.items() if t.done()]
    for wid in dead:
        _ACTIVE_WATCHERS.pop(wid, None)
        _WATCHER_META.pop(wid, None)

    if watch_id is None or watch_id.strip().lower() in ("all", "*"):
        count = 0
        for wid, task in list(_ACTIVE_WATCHERS.items()):
            if not task.done():
                task.cancel()
                count += 1
            _ACTIVE_WATCHERS.pop(wid, None)
            _WATCHER_META.pop(wid, None)
        return count

    target_key = None
    target_norm = watch_id.strip().lower()
    for wid in _ACTIVE_WATCHERS:
        if wid.lower() == target_norm:
            target_key = wid
            break

    if target_key:
        task = _ACTIVE_WATCHERS.pop(target_key, None)
        _WATCHER_META.pop(target_key, None)
        if task and not task.done():
            task.cancel()
            return 1

    return 0
