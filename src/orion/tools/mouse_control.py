"""Remote mouse and screen coordinate interaction module for ORION (Step 28)."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import time

logger = logging.getLogger("orion.tools.mouse_control")

# Windows User32 Mouse Constants
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
WHEEL_DELTA = 120


class POINT(ctypes.Structure):
    """Windows POINT structure for screen coordinates."""

    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


def get_cursor_position() -> tuple[int, int]:
    """Retrieve current screen coordinates of mouse cursor using GetCursorPos."""
    try:
        user32 = ctypes.windll.user32
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        return int(pt.x), int(pt.y)
    except Exception as e:
        logger.warning("Failed to get cursor position: %s", e)
        return 0, 0


def move_cursor(x: int, y: int) -> bool:
    """Move mouse cursor to specific (x, y) screen coordinates using SetCursorPos."""
    try:
        user32 = ctypes.windll.user32
        user32.SetCursorPos(int(x), int(y))
        return True
    except Exception as e:
        logger.warning("Failed to move cursor to (%s, %s): %s", x, y, e)
        return False


def mouse_click(
    x: int | None = None,
    y: int | None = None,
    button: str = "left",
    clicks: int = 1,
) -> bool:
    """Perform single, double, left, right, or middle mouse clicks."""
    try:
        user32 = ctypes.windll.user32

        if x is not None and y is not None:
            move_cursor(x, y)
            time.sleep(0.05)

        btn = button.strip().lower()

        for i in range(clicks):
            if btn == "left":
                user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            elif btn == "right":
                user32.mouse_event(MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
                user32.mouse_event(MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
            elif btn == "middle":
                user32.mouse_event(MOUSEEVENTF_MIDDLEDOWN, 0, 0, 0, 0)
                user32.mouse_event(MOUSEEVENTF_MIDDLEUP, 0, 0, 0, 0)
            else:
                user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

            if clicks > 1 and i < clicks - 1:
                time.sleep(0.05)

        return True
    except Exception as e:
        logger.warning("Failed to perform mouse click (%s, clicks=%s): %s", button, clicks, e)
        return False


def mouse_scroll(steps: int = 3, direction: str = "down") -> bool:
    """Scroll mouse wheel vertically by given step count using mouse_event."""
    try:
        user32 = ctypes.windll.user32
        dir_clean = direction.strip().lower()
        delta = (WHEEL_DELTA * steps) if dir_clean == "up" else (-WHEEL_DELTA * steps)
        user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, delta, 0)
        return True
    except Exception as e:
        logger.warning("Failed to scroll mouse wheel: %s", e)
        return False
