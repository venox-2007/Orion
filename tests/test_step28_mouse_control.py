"""Unit tests for Step 28 — Remote Mouse & Screen Coordinate Interaction."""

import re
from unittest.mock import call, patch
import pytest

from orion.tools.mouse_control import (
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_MIDDLEDOWN,
    MOUSEEVENTF_MIDDLEUP,
    MOUSEEVENTF_RIGHTDOWN,
    MOUSEEVENTF_RIGHTUP,
    MOUSEEVENTF_WHEEL,
    WHEEL_DELTA,
    get_cursor_position,
    mouse_click,
    mouse_scroll,
    move_cursor,
)
from orion.tools.router import (
    check_deterministic_mouse_query,
    route_and_execute_tool,
)


def test_mouse_click_mocked():
    """Verify mouse_click with coordinates moves cursor and dispatches mouse events."""
    with patch("ctypes.windll.user32.SetCursorPos", return_value=1) as mock_set_pos, \
         patch("ctypes.windll.user32.mouse_event") as mock_mouse_event, \
         patch("time.sleep"):

        # 1. Left click at coordinates
        res_left = mouse_click(100, 200, button="left", clicks=1)
        assert res_left is True
        mock_set_pos.assert_called_once_with(100, 200)
        assert mock_mouse_event.call_args_list == [
            call(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0),
            call(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0),
        ]

        # 2. Right click without coordinates
        mock_set_pos.reset_mock()
        mock_mouse_event.reset_mock()

        res_right = mouse_click(button="right", clicks=1)
        assert res_right is True
        mock_set_pos.assert_not_called()
        assert mock_mouse_event.call_args_list == [
            call(MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0),
            call(MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0),
        ]

        # 3. Middle click
        mock_mouse_event.reset_mock()
        res_mid = mouse_click(button="middle", clicks=1)
        assert res_mid is True
        assert mock_mouse_event.call_args_list == [
            call(MOUSEEVENTF_MIDDLEDOWN, 0, 0, 0, 0),
            call(MOUSEEVENTF_MIDDLEUP, 0, 0, 0, 0),
        ]


def test_mouse_double_click_mocked():
    """Verify mouse_click with clicks=2 dispatches 2 down/up event pairs."""
    with patch("ctypes.windll.user32.mouse_event") as mock_mouse_event, \
         patch("time.sleep"):

        res = mouse_click(button="left", clicks=2)
        assert res is True
        assert mock_mouse_event.call_count == 4
        assert mock_mouse_event.call_args_list == [
            call(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0),
            call(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0),
            call(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0),
            call(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0),
        ]


def test_mouse_scroll_mocked():
    """Verify mouse_scroll calculates positive and negative WHEEL_DELTA correctly."""
    with patch("ctypes.windll.user32.mouse_event") as mock_mouse_event:
        # 1. Scroll down 5 steps (negative delta)
        res_down = mouse_scroll(steps=5, direction="down")
        assert res_down is True
        mock_mouse_event.assert_called_once_with(MOUSEEVENTF_WHEEL, 0, 0, -(WHEEL_DELTA * 5), 0)

        # 2. Scroll up 3 steps (positive delta)
        mock_mouse_event.reset_mock()
        res_up = mouse_scroll(steps=3, direction="up")
        assert res_up is True
        mock_mouse_event.assert_called_once_with(MOUSEEVENTF_WHEEL, 0, 0, WHEEL_DELTA * 3, 0)


def test_get_and_move_cursor_mocked():
    """Verify get_cursor_position and move_cursor interact with user32 APIs."""
    with patch("ctypes.windll.user32.SetCursorPos", return_value=1) as mock_set_pos:
        assert move_cursor(500, 600) is True
        mock_set_pos.assert_called_once_with(500, 600)

    # For get_cursor_position, mock GetCursorPos to populate point
    def fake_get_pos(byref_pt):
        # byref_pt._obj is the POINT struct
        byref_pt._obj.x = 800
        byref_pt._obj.y = 450
        return 1

    with patch("ctypes.windll.user32.GetCursorPos", side_effect=fake_get_pos):
        x, y = get_cursor_position()
        assert x == 800
        assert y == 450


@pytest.mark.asyncio
async def test_router_mouse_queries():
    """Verify deterministic routing for click, right-click, double-click, move, scroll, and position."""
    with patch("orion.tools.mouse_control.mouse_click", return_value=True) as mock_click, \
         patch("orion.tools.mouse_control.move_cursor", return_value=True) as mock_move, \
         patch("orion.tools.mouse_control.mouse_scroll", return_value=True) as mock_scroll, \
         patch("orion.tools.mouse_control.get_cursor_position", return_value=(960, 540)):

        # 1. Click at coordinates
        res1 = await route_and_execute_tool("click at 500, 400")
        assert res1 is not None and res1.get("handled") is True
        assert "🖱️ Clicked at (500, 400)." in res1.get("reply", "")
        mock_click.assert_called_once_with(500, 400, button="left", clicks=1)
        assert re.search(r"</?(?:b|i|s|code|pre)>", res1["reply"]) is None

        # 2. Right click
        mock_click.reset_mock()
        res2 = await route_and_execute_tool("right click")
        assert res2 is not None and res2.get("handled") is True
        assert "🖱️ Right-clicked." in res2.get("reply", "")
        mock_click.assert_called_once_with(button="right", clicks=1)

        # Right click at coordinates
        mock_click.reset_mock()
        res2_coords = await route_and_execute_tool("right click at 100, 200")
        assert res2_coords is not None and res2_coords.get("handled") is True
        assert "🖱️ Right-clicked at (100, 200)." in res2_coords.get("reply", "")
        mock_click.assert_called_once_with(100, 200, button="right", clicks=1)

        # 3. Double click at coordinates
        mock_click.reset_mock()
        res3 = await route_and_execute_tool("double click at 200, 300")
        assert res3 is not None and res3.get("handled") is True
        assert "🖱️ Double-clicked at (200, 300)." in res3.get("reply", "")
        mock_click.assert_called_once_with(200, 300, button="left", clicks=2)

        # Double click without coordinates
        mock_click.reset_mock()
        res3_no_coords = await route_and_execute_tool("double click")
        assert res3_no_coords is not None and res3_no_coords.get("handled") is True
        assert "🖱️ Double-clicked." in res3_no_coords.get("reply", "")
        mock_click.assert_called_once_with(button="left", clicks=2)

        # 4. Move cursor
        res4 = await route_and_execute_tool("move cursor to 960, 540")
        assert res4 is not None and res4.get("handled") is True
        assert "🖱️ Moved cursor to (960, 540)." in res4.get("reply", "")
        mock_move.assert_called_once_with(960, 540)

        # Alternative phrasing: "move mouse 100 200"
        mock_move.reset_mock()
        res4_alt = await route_and_execute_tool("move mouse 100 200")
        assert res4_alt is not None and res4_alt.get("handled") is True
        assert "🖱️ Moved cursor to (100, 200)." in res4_alt.get("reply", "")
        mock_move.assert_called_once_with(100, 200)

        # 5. Scroll down 5
        res5 = await route_and_execute_tool("scroll down 5")
        assert res5 is not None and res5.get("handled") is True
        assert "📜 Scrolled down (5 steps)." in res5.get("reply", "")
        mock_scroll.assert_called_once_with(steps=5, direction="down")

        # Scroll up default steps
        mock_scroll.reset_mock()
        res5_up = await route_and_execute_tool("scroll up")
        assert res5_up is not None and res5_up.get("handled") is True
        assert "📜 Scrolled up (3 steps)." in res5_up.get("reply", "")
        mock_scroll.assert_called_once_with(steps=3, direction="up")

        # 6. Where is my mouse
        res6 = await route_and_execute_tool("where is my mouse")
        assert res6 is not None and res6.get("handled") is True
        assert "🖱️ *Cursor Position:* (960, 540)" in res6.get("reply", "")

        # Alternative: "cursor position?"
        res6_alt = await route_and_execute_tool("cursor position?")
        assert res6_alt is not None and res6_alt.get("handled") is True
        assert "🖱️ *Cursor Position:* (960, 540)" in res6_alt.get("reply", "")
