"""Unit tests for Step 29 — Computer Vision & Grounded UI Targeting (Autonomous Element Clicker)."""

import re
from unittest.mock import patch
import pytest

from orion.tools.router import route_and_execute_tool
from orion.tools.ui_locator import (
    click_element_by_name,
    find_element_coordinates,
)


def test_find_element_coordinates_mocked():
    """Verify find_element_coordinates inspects controls via EnumChildWindows and calculates center coords."""
    # 1. Test via EnumChildWindows and user32 mocks
    def fake_enum_child(fg_hwnd, cb, lparam):
        cb(1001, 0)
        return 1

    def fake_get_text(hwnd, buf, maxlen):
        buf.value = "Save"
        return 4

    def fake_get_rect(hwnd, byref_rect):
        byref_rect._obj.left = 100
        byref_rect._obj.top = 100
        byref_rect._obj.right = 200
        byref_rect._obj.bottom = 150
        return 1

    with patch("ctypes.windll.user32.GetForegroundWindow", return_value=9999), \
         patch("ctypes.windll.user32.IsWindowVisible", return_value=1), \
         patch("ctypes.windll.user32.GetWindowTextLengthW", return_value=4), \
         patch("ctypes.windll.user32.GetWindowTextW", side_effect=fake_get_text), \
         patch("ctypes.windll.user32.GetWindowRect", side_effect=fake_get_rect), \
         patch("ctypes.windll.user32.EnumChildWindows", side_effect=fake_enum_child):

        coords = find_element_coordinates("Save")
        assert coords == (150, 125)

    # 2. Test click_element_by_name success
    with patch("orion.tools.ui_locator.find_element_coordinates", return_value=(150, 125)), \
         patch("orion.tools.mouse_control.mouse_click", return_value=True) as mock_click:

        success, msg = click_element_by_name("Save")
        assert success is True
        assert "🖱️ Located and clicked *Save* at (150, 125)." in msg
        mock_click.assert_called_once_with(150, 125, button="left", clicks=1)
        assert re.search(r"</?(?:b|i|s|code|pre)>", msg) is None


def test_click_element_not_found():
    """Verify click_element_by_name handles missing elements gracefully."""
    with patch("orion.tools.ui_locator.find_element_coordinates", return_value=None):
        success, msg = click_element_by_name("Cancel")
        assert success is False
        assert "⚠️ Could not locate UI element *Cancel* on the active screen." in msg
        assert re.search(r"</?(?:b|i|s|code|pre)>", msg) is None

    # Empty target label
    success_empty, msg_empty = click_element_by_name("")
    assert success_empty is False
    assert "No UI element name specified" in msg_empty


@pytest.mark.asyncio
async def test_router_ui_click_queries():
    """Verify router matches natural element click phrasing deterministically."""
    with patch("orion.tools.ui_locator.click_element_by_name") as mock_click_elem:
        mock_click_elem.return_value = (True, "🖱️ Located and clicked *Save* at (150, 125).")

        # 1. "click on Save button"
        res1 = await route_and_execute_tool("click on Save button")
        assert res1 is not None and res1.get("handled") is True
        assert "🖱️ Located and clicked *Save* at (150, 125)." in res1.get("reply", "")
        mock_click_elem.assert_called_once_with("Save")
        assert re.search(r"</?(?:b|i|s|code|pre)>", res1["reply"]) is None

        # 2. "click Submit"
        mock_click_elem.reset_mock()
        mock_click_elem.return_value = (True, "🖱️ Located and clicked *Submit* at (300, 400).")
        res2 = await route_and_execute_tool("click Submit")
        assert res2 is not None and res2.get("handled") is True
        assert "🖱️ Located and clicked *Submit* at (300, 400)." in res2.get("reply", "")
        mock_click_elem.assert_called_once_with("Submit")

        # 3. "click File"
        mock_click_elem.reset_mock()
        mock_click_elem.return_value = (True, "🖱️ Located and clicked *File* at (50, 20).")
        res3 = await route_and_execute_tool("click File")
        assert res3 is not None and res3.get("handled") is True
        assert "🖱️ Located and clicked *File* at (50, 20)." in res3.get("reply", "")
        mock_click_elem.assert_called_once_with("File")

        # 4. Element not found via router
        mock_click_elem.reset_mock()
        mock_click_elem.return_value = (False, "⚠️ Could not locate UI element *MissingBtn* on the active screen.")
        res_missing = await route_and_execute_tool("click MissingBtn")
        assert res_missing is not None and res_missing.get("handled") is True
        assert "⚠️ Could not locate UI element *MissingBtn* on the active screen." in res_missing.get("reply", "")
        mock_click_elem.assert_called_once_with("MissingBtn")


@pytest.mark.asyncio
async def test_app_targeted_ui_clicking():
    """Verify app-targeting syntax 'click <elem> on <app>' and 'click <elem> in <app>' (Step 29.1)."""
    with patch("orion.tools.input_control.focus_window_by_name", return_value=True) as mock_focus, \
         patch("orion.tools.ui_locator.find_element_coordinates", return_value=(340, 240)), \
         patch("orion.tools.mouse_control.mouse_click", return_value=True) as mock_click:

        # 1. "click view on notepad"
        res1 = await route_and_execute_tool("click view on notepad")
        assert res1 is not None and res1.get("handled") is True
        assert "🖱️ Located and clicked *view* in *notepad*." in res1.get("reply", "")
        mock_focus.assert_called_with("notepad")
        mock_click.assert_called_with(340, 240, button="left", clicks=1)
        assert re.search(r"</?(?:b|i|s|code|pre)>", res1["reply"]) is None

        # 2. "click save in notepad"
        mock_focus.reset_mock()
        mock_click.reset_mock()
        res2 = await route_and_execute_tool("click save in notepad")
        assert res2 is not None and res2.get("handled") is True
        assert "🖱️ Located and clicked *save* in *notepad*." in res2.get("reply", "")
        mock_focus.assert_called_with("notepad")

        # 3. Target app window not found
        mock_focus.reset_mock()
        mock_focus.return_value = False
        res_fail = await route_and_execute_tool("click view on missingapp")
        assert res_fail is not None and res_fail.get("handled") is True
        assert "⚠️ Could not find an active window for *missingapp*. Please open it first." in res_fail.get("reply", "")


def test_uiautomation_tree_inspection_mocked():
    """Verify uiautomation walks UI controls and finds MenuItemControl/ButtonControl."""
    from unittest.mock import MagicMock

    mock_rect = MagicMock()
    mock_rect.left = 300
    mock_rect.top = 200
    mock_rect.right = 360
    mock_rect.bottom = 240

    mock_ctrl = MagicMock()
    mock_ctrl.Name = "View"
    mock_ctrl.AutomationId = "ViewMenu"
    mock_ctrl.BoundingRectangle = mock_rect

    mock_win = MagicMock()
    mock_win.Name = "Notepad"
    mock_win.ControlTypeName = "WindowControl"
    mock_win.GetChildren.return_value = []
    mock_win.BoundingRectangle = None

    with patch("uiautomation.GetForegroundControl", return_value=mock_win), \
         patch("uiautomation.GetFocusedControl", return_value=mock_win), \
         patch("uiautomation.WalkControl", return_value=[(mock_ctrl, 1)]):

        coords = find_element_coordinates("View")
        assert coords == (330, 220)

