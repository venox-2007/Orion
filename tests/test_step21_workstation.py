"""Unit tests for Step 21 — Workstation Session, Media Control & Repeat Action Follow-Up."""

import time
from unittest.mock import patch
import pytest


def test_workstation_lock_mocked():
    """Verify lock_workstation calls LockWorkStation and handles result."""
    from orion.tools.workstation import lock_workstation

    with patch("ctypes.windll.user32.LockWorkStation", return_value=1) as mock_lock:
        res = lock_workstation()
        assert res is True
        mock_lock.assert_called_once()

    with patch("ctypes.windll.user32.LockWorkStation", return_value=0):
        with pytest.raises(OSError):
            lock_workstation()


def test_media_keys_mocked():
    """Verify media control functions invoke keybd_event with expected VK codes and KEYUP."""
    from orion.tools.workstation import (
        KEYEVENTF_KEYUP,
        VK_MEDIA_NEXT_TRACK,
        VK_MEDIA_PLAY_PAUSE,
        VK_MEDIA_PREV_TRACK,
        VK_VOLUME_MUTE,
        next_track,
        previous_track,
        toggle_mute,
        toggle_play_pause,
    )

    with patch("ctypes.windll.user32.keybd_event") as mock_keybd:
        # 1. Play/Pause
        assert toggle_play_pause() is True
        assert mock_keybd.call_count == 2
        mock_keybd.assert_any_call(VK_MEDIA_PLAY_PAUSE, 0, 0, 0)
        mock_keybd.assert_any_call(VK_MEDIA_PLAY_PAUSE, 0, KEYEVENTF_KEYUP, 0)

        # 2. Next track
        mock_keybd.reset_mock()
        assert next_track() is True
        assert mock_keybd.call_count == 2
        mock_keybd.assert_any_call(VK_MEDIA_NEXT_TRACK, 0, 0, 0)
        mock_keybd.assert_any_call(VK_MEDIA_NEXT_TRACK, 0, KEYEVENTF_KEYUP, 0)

        # 3. Previous track
        mock_keybd.reset_mock()
        assert previous_track() is True
        assert mock_keybd.call_count == 2
        mock_keybd.assert_any_call(VK_MEDIA_PREV_TRACK, 0, 0, 0)
        mock_keybd.assert_any_call(VK_MEDIA_PREV_TRACK, 0, KEYEVENTF_KEYUP, 0)

        # 4. Mute
        mock_keybd.reset_mock()
        assert toggle_mute() is True
        assert mock_keybd.call_count == 2
        mock_keybd.assert_any_call(VK_VOLUME_MUTE, 0, 0, 0)
        mock_keybd.assert_any_call(VK_VOLUME_MUTE, 0, KEYEVENTF_KEYUP, 0)


def test_volume_adjust_steps():
    """Verify adjust_volume simulates down and up key events matching steps."""
    from orion.tools.workstation import (
        KEYEVENTF_KEYUP,
        VK_VOLUME_DOWN,
        VK_VOLUME_UP,
        adjust_volume,
    )

    with patch("ctypes.windll.user32.keybd_event") as mock_keybd:
        # Up with 3 steps -> 3 down + 3 up = 6 keybd_event calls
        assert adjust_volume("up", steps=3) is True
        assert mock_keybd.call_count == 6
        down_calls = [c for c in mock_keybd.call_args_list if c == ((VK_VOLUME_UP, 0, 0, 0),)]
        up_calls = [c for c in mock_keybd.call_args_list if c == ((VK_VOLUME_UP, 0, KEYEVENTF_KEYUP, 0),)]
        assert len(down_calls) == 3
        assert len(up_calls) == 3

        # Down with 2 steps -> 2 down + 2 up = 4 keybd_event calls
        mock_keybd.reset_mock()
        assert adjust_volume("down", steps=2) is True
        assert mock_keybd.call_count == 4
        down_calls = [c for c in mock_keybd.call_args_list if c == ((VK_VOLUME_DOWN, 0, 0, 0),)]
        up_calls = [c for c in mock_keybd.call_args_list if c == ((VK_VOLUME_DOWN, 0, KEYEVENTF_KEYUP, 0),)]
        assert len(down_calls) == 2
        assert len(up_calls) == 2

        # Invalid direction raises ValueError
        with pytest.raises(ValueError):
            adjust_volume("diagonal", steps=1)


@pytest.mark.asyncio
async def test_deterministic_workstation_routing():
    """Verify workstation queries route deterministically and return valid reply text."""
    from orion.tools.router import check_deterministic_workstation_query, route_and_execute_tool

    cases = [
        ("lock my laptop", "🔒 Laptop locked successfully."),
        ("pause", "⏯️ Toggled media playback."),
        ("resume", "⏯️ Toggled media playback."),
        ("skip track", "⏭️ Skipped to next track."),
        ("mute", "🔇 Toggled mute."),
        ("volume up", "🔊 Volume increased."),
        ("volume down", "🔉 Volume decreased."),
    ]

    with patch("ctypes.windll.user32.LockWorkStation", return_value=1), \
         patch("ctypes.windll.user32.keybd_event"):
        for query, expected_reply in cases:
            # 1. Direct query check
            ctx: dict = {}
            res = check_deterministic_workstation_query(query, context=ctx)
            assert res is not None, f"Query '{query}' returned None"
            assert res.get("handled") is True
            assert res.get("reply") == expected_reply
            assert "last_action" in ctx
            assert ctx["last_action"]["type"] == "workstation"

            # 2. Router check
            ctx2: dict = {}
            router_res = await route_and_execute_tool(query, context=ctx2)
            assert router_res is not None, f"Router for '{query}' returned None"
            assert router_res.get("handled") is True
            assert router_res.get("reply") == expected_reply


@pytest.mark.asyncio
async def test_repeat_followup_screenshot():
    """Verify repeat query re-triggers desktop screenshot capture when within context window."""
    from orion.tools.router import check_deterministic_repeat_query, route_and_execute_tool

    fake_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF"
    with patch("orion.tools.screen_capture.take_desktop_screenshot", return_value=fake_bytes) as mock_shot:
        ctx = {
            "last_action": {
                "type": "screenshot",
                "timestamp": time.time() - 10,
            }
        }

        # Check "again" directly
        res = check_deterministic_repeat_query("again", context=ctx)
        assert res is not None
        assert res.get("handled") is True
        assert res.get("type") == "photo"
        assert res.get("photo_bytes") == fake_bytes
        mock_shot.assert_called_once()

        # Check "one more" via router
        mock_shot.reset_mock()
        router_res = await route_and_execute_tool("one more", context=ctx)
        assert router_res is not None
        assert router_res.get("handled") is True
        assert router_res.get("type") == "photo"
        assert router_res.get("photo_bytes") == fake_bytes
        mock_shot.assert_called_once()


@pytest.mark.asyncio
async def test_repeat_followup_expired():
    """Verify repeat query does not trigger previous action when timestamp > 5 minutes."""
    from orion.tools.router import check_deterministic_repeat_query, route_and_execute_tool

    ctx = {
        "last_action": {
            "type": "screenshot",
            "timestamp": time.time() - 301,
        }
    }

    # Direct repeat check returns None
    res = check_deterministic_repeat_query("again", context=ctx)
    assert res is None

    # Router does not call screenshot
    with patch("orion.tools.screen_capture.take_desktop_screenshot") as mock_shot:
        router_res = await route_and_execute_tool("again", context=ctx)
        mock_shot.assert_not_called()
        if router_res:
            assert router_res.get("type") != "photo"


def test_repeat_followup_workstation():
    """Verify repeat query re-executes workstation action."""
    from orion.tools.router import check_deterministic_repeat_query

    with patch("ctypes.windll.user32.keybd_event") as mock_keybd:
        ctx = {
            "last_action": {
                "type": "workstation",
                "action": "volume_up",
                "timestamp": time.time() - 15,
            }
        }
        res = check_deterministic_repeat_query("repeat", context=ctx)
        assert res is not None
        assert res.get("handled") is True
        assert res.get("reply") == "🔊 Volume increased."
        # volume_up with steps=5 calls keybd_event 10 times (5 down, 5 up)
        assert mock_keybd.call_count == 10
