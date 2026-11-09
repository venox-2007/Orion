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

        # 3. Target app window focus failed
        mock_focus.reset_mock()
        mock_focus.return_value = False
        res_fail = await route_and_execute_tool("click view on missingapp")
        assert res_fail is not None and res_fail.get("handled") is True
        assert "⚠️ Found *missingapp*, but could not bring it to the foreground." in res_fail.get("reply", "")


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


def test_force_foreground_window_mocked():
    """Verify force_foreground_window handles active, iconic, and thread attachment."""
    from orion.tools.input_control import force_foreground_window

    # 1. Already active
    with patch("ctypes.windll.user32.GetForegroundWindow", return_value=1234):
        assert force_foreground_window(1234) is True

    # 2. Iconic (minimized) -> restored with SW_RESTORE (9) and thread attachment
    with patch("ctypes.windll.user32.GetForegroundWindow", side_effect=[9999, 9999, 1234, 1234]), \
         patch("ctypes.windll.user32.IsIconic", return_value=1) as mock_iconic, \
         patch("ctypes.windll.user32.ShowWindow") as mock_show, \
         patch("ctypes.windll.user32.GetWindowThreadProcessId", return_value=100), \
         patch("ctypes.windll.kernel32.GetCurrentThreadId", return_value=200), \
         patch("ctypes.windll.user32.AttachThreadInput") as mock_attach, \
         patch("ctypes.windll.user32.BringWindowToTop") as mock_top, \
         patch("ctypes.windll.user32.SetForegroundWindow") as mock_set_fg:

        res = force_foreground_window(1234)
        assert res is True
        mock_iconic.assert_called_with(1234)
        mock_show.assert_called_with(1234, 9)
        mock_attach.assert_any_call(200, 100, True)
        mock_attach.assert_any_call(200, 100, False)
        mock_top.assert_called_with(1234)
        mock_set_fg.assert_called_with(1234)


def test_programmatic_invoke_and_fallback():
    """Verify programmatic invoke pattern first, with fallback to coordinate click."""
    from unittest.mock import MagicMock
    from orion.tools.ui_locator import click_element_by_name

    # 1. Successful GetInvokePattern().Invoke()
    mock_pattern = MagicMock()
    mock_ctrl = MagicMock()
    mock_ctrl.GetInvokePattern.return_value = mock_pattern

    with patch("orion.tools.input_control.focus_window_by_name", return_value=True), \
         patch("orion.tools.ui_locator._find_uia_control", return_value=mock_ctrl):
        success, msg = click_element_by_name("Save", target_app="Notepad")
        assert success is True
        assert msg == "🖱️ Clicked *Save* in *Notepad*."
        mock_pattern.Invoke.assert_called_once()

    # 2. Successful Click(simulateMove=False) when Invoke pattern is unavailable
    mock_ctrl2 = MagicMock()
    mock_ctrl2.GetInvokePattern.side_effect = Exception("No invoke pattern")
    mock_ctrl2.Click = MagicMock()

    with patch("orion.tools.ui_locator._find_uia_control", return_value=mock_ctrl2):
        success2, msg2 = click_element_by_name("File")
        assert success2 is True
        assert msg2 == "🖱️ Clicked *File* in *active window*."
        mock_ctrl2.Click.assert_called_once_with(simulateMove=False)

    # 3. Fallback to coordinate click when direct invoke raises
    mock_ctrl3 = MagicMock()
    mock_ctrl3.GetInvokePattern.side_effect = Exception("No pattern")
    mock_ctrl3.Click.side_effect = Exception("Click failed")

    with patch("orion.tools.input_control.focus_window_by_name", return_value=True), \
         patch("orion.tools.ui_locator._find_uia_control", return_value=mock_ctrl3), \
         patch("orion.tools.ui_locator.find_element_coordinates", return_value=(150, 250)), \
         patch("orion.tools.mouse_control.mouse_click") as mock_mouse_click:
        success3, msg3 = click_element_by_name("Options", target_app="Settings")
        assert success3 is True
        assert msg3 == "🖱️ Located and clicked *Options* in *Settings*."
        mock_mouse_click.assert_called_once_with(150, 250, button="left", clicks=1)

