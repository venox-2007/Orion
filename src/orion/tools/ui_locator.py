"""Computer vision & grounded UI element locator for ORION (Step 29)."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
from typing import Any

logger = logging.getLogger("orion.tools.ui_locator")


class RECT(ctypes.Structure):
    """Windows RECT structure for bounding coordinates."""

    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


def _get_control_bounds_uia(clean_label: str) -> tuple[int, int, int, int] | None:
    """Attempt to locate UI element using the Windows UI Automation library if available."""
    try:
        import uiautomation as auto  # type: ignore

        # 1. Check foreground control first
        fg_ctrl = auto.GetForegroundControl()
        if fg_ctrl:
            target = fg_ctrl.Control(
                searchDepth=3,
                Compare=lambda c, d: clean_label in c.Name.lower() or clean_label in c.AutomationId.lower(),
            )
            if target and target.Exists(maxSearchSeconds=0.3):
                rect = target.BoundingRectangle
                if rect and rect.right > rect.left and rect.bottom > rect.top:
                    return rect.left, rect.top, rect.right, rect.bottom

        # 2. Check root desktop controls
        root = auto.GetRootControl()
        if root:
            target = root.Control(
                searchDepth=2,
                Compare=lambda c, d: clean_label in c.Name.lower() or clean_label in c.AutomationId.lower(),
            )
            if target and target.Exists(maxSearchSeconds=0.3):
                rect = target.BoundingRectangle
                if rect and rect.right > rect.left and rect.bottom > rect.top:
                    return rect.left, rect.top, rect.right, rect.bottom
    except Exception as e:
        logger.debug("UIAutomation library lookup skipped: %s", e)

    return None


def _get_control_bounds_native(clean_label: str) -> tuple[int, int, int, int] | None:
    """Enumerate child controls and windows via Win32 user32 APIs to find matching bounds."""
    try:
        user32 = ctypes.windll.user32
        fg_hwnd = user32.GetForegroundWindow()
        found_rect: tuple[int, int, int, int] | None = None

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def check_window_match(hwnd: int) -> bool:
            nonlocal found_rect
            if hasattr(user32, "IsWindowVisible") and not user32.IsWindowVisible(hwnd):
                return False

            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                txt = buf.value.strip().lower()
                if clean_label in txt or txt in clean_label:
                    rect = RECT()
                    if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                        if rect.right > rect.left and rect.bottom > rect.top:
                            found_rect = (rect.left, rect.top, rect.right, rect.bottom)
                            return True
            return False

        def enum_child_proc(hwnd: int, lparam: int) -> bool:
            if check_window_match(hwnd):
                return False
            return True

        child_cb = WNDENUMPROC(enum_child_proc)

        # 1. Check children of foreground window
        if fg_hwnd:
            if check_window_match(fg_hwnd):
                return found_rect
            user32.EnumChildWindows(fg_hwnd, child_cb, 0)
            if found_rect:
                return found_rect

        # 2. Check all top-level windows and their children if not found in foreground window
        def enum_win_proc(hwnd: int, lparam: int) -> bool:
            nonlocal found_rect
            if check_window_match(hwnd):
                return False
            user32.EnumChildWindows(hwnd, child_cb, 0)
            if found_rect:
                return False
            return True

        win_cb = WNDENUMPROC(enum_win_proc)
        user32.EnumWindows(win_cb, 0)

        return found_rect
    except Exception as e:
        logger.debug("Native control enumeration failed: %s", e)
        return None


def find_element_coordinates(target_label: str) -> tuple[int, int] | None:
    """Find the center coordinates (x, y) of a UI element matching target_label."""
    clean_label = target_label.strip().lower()
    if not clean_label:
        return None

    # 1. Try Windows UI Automation if available
    bounds = _get_control_bounds_uia(clean_label)

    # 2. Fall back to native Win32 child window and control enumeration
    if bounds is None:
        bounds = _get_control_bounds_native(clean_label)

    if bounds is not None:
        left, top, right, bottom = bounds
        center_x = (left + right) // 2
        center_y = (top + bottom) // 2
        return center_x, center_y

    return None


def click_element_by_name(
    target_label: str,
    clicks: int = 1,
    button: str = "left",
) -> tuple[bool, str]:
    """Locate UI element on active screen and click at its center coordinates."""
    clean_label = target_label.strip()
    if not clean_label:
        return False, "⚠️ No UI element name specified."

    coords = find_element_coordinates(clean_label)
    if coords is not None:
        x, y = coords
        from orion.tools import mouse_control

        mouse_control.mouse_click(x, y, button=button, clicks=clicks)
        return True, f"🖱️ Located and clicked *{clean_label}* at ({x}, {y})."

    return False, f"⚠️ Could not locate UI element *{clean_label}* on the active screen."
