"""Computer vision & grounded UI element locator for ORION (Steps 29 & 29.1)."""

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


def _find_uia_control(clean_label: str) -> Any | None:
    """Locate UI element control using the Windows UI Automation library (prioritized)."""
    try:
        import uiautomation as auto  # type: ignore

        # 1. Prioritize active foreground or focused window
        search_roots = []
        try:
            fg_ctrl = auto.GetForegroundControl() or auto.GetFocusedControl()
            if fg_ctrl:
                top_win = fg_ctrl
                curr = fg_ctrl
                for _ in range(8):
                    if curr is None:
                        break
                    if getattr(curr, "ControlTypeName", "") == "WindowControl":
                        top_win = curr
                        break
                    try:
                        curr = curr.GetParentControl()
                    except Exception:
                        break
                search_roots.append(top_win)
                if fg_ctrl != top_win:
                    search_roots.append(fg_ctrl)
        except Exception as e:
            logger.debug("Foreground control inspection error: %s", e)

        # 2. Also inspect active top-level WindowControls from root
        try:
            root = auto.GetRootControl()
            if root:
                for win in root.GetChildren():
                    if win not in search_roots:
                        search_roots.append(win)
        except Exception as e:
            logger.debug("Root children inspection error: %s", e)

        best_candidate: Any | None = None

        for root_ctrl in search_roots:
            if not root_ctrl:
                continue

            # Check root control itself
            try:
                root_name = (root_ctrl.Name or "").strip().lower()
                if root_name == clean_label:
                    return root_ctrl
            except Exception:
                pass

            # Walk child controls up to depth 5 (supports XAML/WinUI 3 MenuItemControl, ButtonControl, etc.)
            try:
                for ctrl, depth in auto.WalkControl(root_ctrl, maxDepth=5):
                    if ctrl is None:
                        continue
                    try:
                        name = (ctrl.Name or "").strip().lower()
                        auto_id = (ctrl.AutomationId or "").strip().lower()

                        # Prefer exact match
                        if name == clean_label:
                            return ctrl

                        # Substring match (e.g. "File", "Edit", "View", or button text)
                        elif clean_label in name or (auto_id and clean_label in auto_id):
                            if best_candidate is None:
                                best_candidate = ctrl
                    except Exception:
                        continue
            except Exception:
                continue

        if best_candidate is not None:
            return best_candidate

    except Exception as e:
        logger.debug("UIAutomation library lookup error: %s", e)

    return None


def _get_control_bounds_uia(clean_label: str) -> tuple[int, int, int, int] | None:
    """Locate UI element bounds using the Windows UI Automation library."""
    ctrl = _find_uia_control(clean_label)
    if ctrl is not None:
        try:
            r = ctrl.BoundingRectangle
            if r and r.right > r.left and r.bottom > r.top:
                return r.left, r.top, r.right, r.bottom
        except Exception:
            pass
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

    # 1. Try Windows UI Automation if available (prioritized for modern Windows 11 apps)
    bounds = _get_control_bounds_uia(clean_label)

    # 2. Fall back to native Win32 child window and control enumeration
    if bounds is None:
        bounds = _get_control_bounds_native(clean_label)

    # 3. State reset: if element not found, a previous click might have left a menu open; tap ESC and retry
    if bounds is None:
        try:
            from orion.tools import input_control

            input_control.press_special_key("escape")
            import time

            time.sleep(0.05)
            bounds = _get_control_bounds_uia(clean_label)
            if bounds is None:
                bounds = _get_control_bounds_native(clean_label)
        except Exception:
            pass

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
    target_app: str | None = None,
) -> tuple[bool, str]:
    """Locate UI element on active screen and invoke or click it."""
    clean_label = target_label.strip()
    if not clean_label:
        return False, "⚠️ No UI element name specified."

    from orion.tools import input_control

    # 1. Target app activation
    if target_app:
        focused = input_control.focus_window_by_name(target_app)
        if not focused:
            return False, f"⚠️ Found *{target_app}*, but could not bring it to the foreground."

    # 2. Native UIAutomation Action Pattern First
    if button == "left" and clicks == 1:
        ctrl = _find_uia_control(clean_label.lower())
        if ctrl is not None:
            invoked = False
            try:
                pattern = ctrl.GetInvokePattern() if hasattr(ctrl, "GetInvokePattern") else None
                if pattern is not None and hasattr(pattern, "Invoke"):
                    pattern.Invoke()
                    invoked = True
            except Exception:
                invoked = False

            if not invoked:
                try:
                    if hasattr(ctrl, "Click"):
                        ctrl.Click(simulateMove=False)
                        invoked = True
                except Exception:
                    invoked = False

            if invoked:
                return True, f"🖱️ Clicked *{clean_label}* in *{target_app or 'active window'}*."

    # 3. Coordinate Click Fallback
    coords = find_element_coordinates(clean_label)

    if coords is not None:
        x, y = coords
        from orion.tools import mouse_control

        mouse_control.mouse_click(x, y, button=button, clicks=clicks)
        if target_app:
            return True, f"🖱️ Located and clicked *{clean_label}* in *{target_app}*."
        return True, f"🖱️ Located and clicked *{clean_label}* at ({x}, {y})."

    # 4. OCR Fallback (Step 32): when UIAutomation controls are unavailable
    try:
        from orion.tools import screen_ocr

        ocr_coords = screen_ocr.find_text_coordinates_sync(clean_label, target_app=target_app)
        if ocr_coords is not None:
            cx, cy = ocr_coords
            from orion.tools import mouse_control

            mouse_control.mouse_click(cx, cy, button=button, clicks=clicks)
            return True, f"👁️ OCR-located and clicked *{clean_label}* at ({cx}, {cy})."
    except Exception as ocr_err:
        logger.debug("OCR fallback search error for '%s': %s", clean_label, ocr_err)

    if target_app:
        return False, f"⚠️ Could not locate UI element *{clean_label}* in *{target_app}*."
    return False, f"⚠️ Could not locate UI element *{clean_label}* on the active screen."
