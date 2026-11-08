"""Unit tests for Step 25 — Remote Clipboard & Text/Keystroke Injection (Virtual Input)."""

import re
from unittest.mock import MagicMock, call, patch
import pytest

from orion.tools.input_control import (
    VK_ESCAPE,
    VK_RETURN,
    VK_TAB,
    get_clipboard_text,
    get_last_target_app,
    press_special_key,
    set_clipboard_text,
    set_last_target_app,
    type_text,
)
from orion.tools.router import (
    check_deterministic_input_query,
    route_and_execute_tool,
)


@pytest.fixture(autouse=True)
def reset_last_target_app():
    set_last_target_app(None)
    yield
    set_last_target_app(None)


def test_clipboard_get_and_set_mocked():
    """Verify get_clipboard_text and set_clipboard_text read and write clipboard data."""
    # 1. Test get_clipboard_text with content
    with patch("ctypes.windll.user32.OpenClipboard", return_value=1), \
         patch("ctypes.windll.user32.GetClipboardData", return_value=0x1234), \
         patch("ctypes.windll.kernel32.GlobalLock", return_value=0x5678), \
         patch("ctypes.wstring_at", return_value="Secret Token 42"), \
         patch("ctypes.windll.kernel32.GlobalUnlock"), \
         patch("ctypes.windll.user32.CloseClipboard"):
        content = get_clipboard_text()
        assert content == "Secret Token 42"

    # 2. Test get_clipboard_text when empty or inaccessible
    with patch("ctypes.windll.user32.OpenClipboard", return_value=0):
        content_empty = get_clipboard_text()
        assert content_empty == ""

    # 3. Test set_clipboard_text
    with patch("ctypes.windll.user32.OpenClipboard", return_value=1), \
         patch("ctypes.windll.user32.EmptyClipboard", return_value=1), \
         patch("ctypes.windll.kernel32.GlobalAlloc", return_value=0x1111), \
         patch("ctypes.windll.kernel32.GlobalLock", return_value=0x2222), \
         patch("ctypes.memmove"), \
         patch("ctypes.windll.kernel32.GlobalUnlock"), \
         patch("ctypes.windll.user32.SetClipboardData", return_value=0x1111), \
         patch("ctypes.windll.user32.CloseClipboard"):
        success = set_clipboard_text("Hello Orion!")
        assert success is True


def test_type_text_mocked():
    """Verify type_text dispatches SendInput events for each character in string."""
    text_to_type = "Hello World!"
    with patch("ctypes.windll.user32.SendInput") as mock_send_input:
        res = type_text(text_to_type)
        assert res is True
        # SendInput is called once per character
        assert mock_send_input.call_count == len(text_to_type)

    # Empty string should succeed without calling SendInput
    with patch("ctypes.windll.user32.SendInput") as mock_send_input2:
        assert type_text("") is True
        mock_send_input2.assert_not_called()


def test_press_special_keys_mocked():
    """Verify enter, escape, and tab call keybd_event with correct VK codes."""
    from orion.tools.input_control import KEYEVENTF_KEYUP

    with patch("ctypes.windll.user32.keybd_event") as mock_keybd:
        # 1. Enter
        assert press_special_key("enter") is True
        assert mock_keybd.call_args_list == [
            call(VK_RETURN, 0, 0, 0),
            call(VK_RETURN, 0, KEYEVENTF_KEYUP, 0),
        ]

        # 2. Escape
        mock_keybd.reset_mock()
        assert press_special_key("escape") is True
        assert mock_keybd.call_args_list == [
            call(VK_ESCAPE, 0, 0, 0),
            call(VK_ESCAPE, 0, KEYEVENTF_KEYUP, 0),
        ]

        # 3. Tab
        mock_keybd.reset_mock()
        assert press_special_key("tab") is True
        assert mock_keybd.call_args_list == [
            call(VK_TAB, 0, 0, 0),
            call(VK_TAB, 0, KEYEVENTF_KEYUP, 0),
        ]

        # 4. Esc alias
        mock_keybd.reset_mock()
        assert press_special_key("esc") is True
        assert mock_keybd.call_args_list == [
            call(VK_ESCAPE, 0, 0, 0),
            call(VK_ESCAPE, 0, KEYEVENTF_KEYUP, 0),
        ]

        # 5. Single alphanumeric key (e.g. "d")
        mock_keybd.reset_mock()
        assert press_special_key("d") is True
        assert mock_keybd.call_args_list == [
            call(0x44, 0, 0, 0),
            call(0x44, 0, KEYEVENTF_KEYUP, 0),
        ]

        # 6. Unsupported key returns False
        mock_keybd.reset_mock()
        assert press_special_key("f13") is False
        mock_keybd.assert_not_called()


@pytest.mark.asyncio
async def test_router_input_queries():
    """Verify router handles clipboard inspection, copy, typing, and special keys deterministically."""
    # 1. Get clipboard content
    with patch("orion.tools.input_control.get_clipboard_text", return_value="Sample copied snippet"):
        res = await route_and_execute_tool("what's in my clipboard")
        assert res is not None
        assert res.get("handled") is True
        assert "📋 *Clipboard Content:*" in res.get("reply", "")
        assert "Sample copied snippet" in res.get("reply", "")
        assert re.search(r"</?(?:b|i|s|code|pre)>", res["reply"]) is None

        # Precedence test: "get clipboard" routes to input control, not file fetch
        res_get = await route_and_execute_tool("get clipboard")
        assert res_get is not None
        assert res_get.get("handled") is True
        assert "📋 *Clipboard Content:*" in res_get.get("reply", "")
        assert res_get.get("type") != "document"

        # Missing apostrophe test: "whats in my clipboard"
        res_whats = await route_and_execute_tool("whats in my clipboard")
        assert res_whats is not None
        assert res_whats.get("handled") is True
        assert "📋 *Clipboard Content:*" in res_whats.get("reply", "")

        # Standalone "clipboard"
        res_clip = await route_and_execute_tool("clipboard")
        assert res_clip is not None
        assert res_clip.get("handled") is True
        assert "📋 *Clipboard Content:*" in res_clip.get("reply", "")

        # "check clipboard"
        res_check = await route_and_execute_tool("check clipboard")
        assert res_check is not None
        assert res_check.get("handled") is True
        assert "📋 *Clipboard Content:*" in res_check.get("reply", "")

    # Get empty clipboard
    with patch("orion.tools.input_control.get_clipboard_text", return_value=""):
        res_empty = await route_and_execute_tool("show clipboard")
        assert res_empty is not None
        assert res_empty.get("handled") is True
        assert "📋 *Clipboard is currently empty.*" in res_empty.get("reply", "")

    # 2. Set clipboard content ("copy hello")
    with patch("orion.tools.input_control.set_clipboard_text", return_value=True) as mock_set:
        res_copy = await route_and_execute_tool("copy hello")
        assert res_copy is not None
        assert res_copy.get("handled") is True
        assert "📋 Copied to laptop clipboard." in res_copy.get("reply", "")
        mock_set.assert_called_once_with("hello")
        assert re.search(r"</?(?:b|i|s|code|pre)>", res_copy["reply"]) is None

        # Alternative copy phrases
        mock_set.reset_mock()
        res_copy2 = await route_and_execute_tool("copy to clipboard: https://example.com")
        assert res_copy2 is not None and res_copy2.get("handled") is True
        mock_set.assert_called_once_with("https://example.com")

        mock_set.reset_mock()
        res_copy3 = await route_and_execute_tool("set clipboard to api_key_12345")
        assert res_copy3 is not None and res_copy3.get("handled") is True
        mock_set.assert_called_once_with("api_key_12345")

    # 3. Type text into active window ("type test message")
    with patch("orion.tools.input_control.type_text", return_value=True) as mock_type:
        res_type = await route_and_execute_tool("type test message")
        assert res_type is not None
        assert res_type.get("handled") is True
        assert "⌨️ Typed text into active window." in res_type.get("reply", "")
        mock_type.assert_called_once_with("test message")
        assert re.search(r"</?(?:b|i|s|code|pre)>", res_type["reply"]) is None

        # Alternative type phrases
        mock_type.reset_mock()
        res_type2 = await route_and_execute_tool("type: npm install express")
        assert res_type2 is not None and res_type2.get("handled") is True
        mock_type.assert_called_once_with("npm install express")

        mock_type.reset_mock()
        res_type3 = await route_and_execute_tool("inject text sudo apt update")
        assert res_type3 is not None and res_type3.get("handled") is True
        mock_type.assert_called_once_with("sudo apt update")

        # Single character typing: "type d"
        mock_type.reset_mock()
        res_type_d = await route_and_execute_tool("type d")
        assert res_type_d is not None and res_type_d.get("handled") is True
        mock_type.assert_called_once_with("d")

        # Quoted string with window suffix: 'type "hello" in notepad'
        mock_type.reset_mock()
        with patch("orion.tools.input_control.focus_window_by_name", return_value=True) as mock_focus:
            res_type_quote = await route_and_execute_tool('type "hello" in notepad')
            assert res_type_quote is not None and res_type_quote.get("handled") is True
            assert "Typed text into *notepad*" in res_type_quote.get("reply", "")
            mock_focus.assert_called_once_with("notepad")
            mock_type.assert_called_once_with("hello")

    # 4. Press special key ("press enter")
    with patch("orion.tools.input_control.press_special_key", return_value=True) as mock_press:
        res_key = await route_and_execute_tool("press enter")
        assert res_key is not None
        assert res_key.get("handled") is True
        assert "🔘 Pressed *ENTER*." in res_key.get("reply", "")
        mock_press.assert_called_once_with("enter")
        assert re.search(r"</?(?:b|i|s|code|pre)>", res_key["reply"]) is None

        mock_press.reset_mock()
        res_esc = await route_and_execute_tool("hit esc")
        assert res_esc is not None and res_esc.get("handled") is True
        assert "🔘 Pressed *ESC*." in res_esc.get("reply", "")
        mock_press.assert_called_once_with("esc")

        # Press single alphanumeric key: "press d"
        mock_press.reset_mock()
        res_press_d = await route_and_execute_tool("press d")
        assert res_press_d is not None and res_press_d.get("handled") is True
        assert "🔘 Pressed *D*." in res_press_d.get("reply", "")
        mock_press.assert_called_once_with("d")


@pytest.mark.asyncio
async def test_compound_type_and_press_key():
    """Verify compound commands 'type <text> and press <key>' type text then press key."""
    with patch("orion.tools.input_control.type_text", return_value=True) as mock_type, \
         patch("orion.tools.input_control.press_special_key", return_value=True) as mock_press:

        # 1. "type d and press enter"
        res1 = await route_and_execute_tool("type d and press enter")
        assert res1 is not None
        assert res1.get("handled") is True
        assert "⌨️ Typed *d* and pressed *ENTER*." in res1.get("reply", "")
        mock_type.assert_called_once_with("d")
        mock_press.assert_called_once_with("enter")
        assert re.search(r"</?(?:b|i|s|code|pre)>", res1["reply"]) is None

        # 2. Compound with window target: 'type "login" and hit enter in chrome'
        mock_type.reset_mock()
        mock_press.reset_mock()
        with patch("orion.tools.input_control.focus_window_by_name", return_value=True) as mock_focus:
            res2 = await route_and_execute_tool('type "login" and hit enter in chrome')
            assert res2 is not None
            assert res2.get("handled") is True
            assert "⌨️ Typed *login* and pressed *ENTER* in *chrome*." in res2.get("reply", "")
            mock_focus.assert_called_once_with("chrome")
            mock_type.assert_called_once_with("login")
            mock_press.assert_called_once_with("enter")


@pytest.mark.asyncio
async def test_typing_with_window_focus():
    """Verify typing with target window attempts to focus target before typing, or reports missing window."""
    with patch("orion.tools.input_control.type_text", return_value=True) as mock_type:
        # 1. Target window found and focused
        with patch("orion.tools.input_control.focus_window_by_name", return_value=True) as mock_focus:
            res_found = await route_and_execute_tool('type "hello" in notepad')
            assert res_found is not None
            assert res_found.get("handled") is True
            assert "⌨️ Typed text into *notepad*." in res_found.get("reply", "")
            mock_focus.assert_called_once_with("notepad")
            mock_type.assert_called_once_with("hello")
            assert re.search(r"</?(?:b|i|s|code|pre)>", res_found["reply"]) is None

        # 2. Target window not found
        mock_type.reset_mock()
        with patch("orion.tools.input_control.focus_window_by_name", return_value=False) as mock_focus_fail:
            res_fail = await route_and_execute_tool('type "ghee khtm" in notepad')
            assert res_fail is not None
            assert res_fail.get("handled") is True
            assert "⚠️ Could not find an active window for *notepad*. Please open it first." in res_fail.get("reply", "")
            mock_focus_fail.assert_called_once_with("notepad")
            mock_type.assert_not_called()
            assert re.search(r"</?(?:b|i|s|code|pre)>", res_fail["reply"]) is None


@pytest.mark.asyncio
async def test_persistent_window_targeting():
    """Verify subsequent input commands inherit the last focused target window."""
    set_last_target_app("notepad")
    assert get_last_target_app() == "notepad"

    with patch("orion.tools.input_control.focus_window_by_name", return_value=True) as mock_focus, \
         patch("orion.tools.input_control.type_text", return_value=True) as mock_type, \
         patch("orion.tools.input_control.press_special_key", return_value=True) as mock_press:

        # 1. Standard typing inherits last target window
        res1 = await route_and_execute_tool('type "ghee khtm"')
        assert res1 is not None and res1.get("handled") is True
        assert "⌨️ Typed text into *notepad*." in res1.get("reply", "")
        mock_focus.assert_called_once_with("notepad")
        mock_type.assert_called_once_with("ghee khtm")

        # 2. Compound typing inherits last target window
        mock_focus.reset_mock()
        mock_type.reset_mock()
        res2 = await route_and_execute_tool("type d and press enter")
        assert res2 is not None and res2.get("handled") is True
        assert "⌨️ Typed *d* and pressed *ENTER* in *notepad*." in res2.get("reply", "")
        mock_focus.assert_called_once_with("notepad")
        mock_type.assert_called_once_with("d")
        mock_press.assert_called_once_with("enter")

        # 3. Special keypress inherits last target window
        mock_focus.reset_mock()
        mock_press.reset_mock()
        res3 = await route_and_execute_tool("press enter")
        assert res3 is not None and res3.get("handled") is True
        assert "🔘 Pressed *ENTER* in *notepad*." in res3.get("reply", "")
        mock_focus.assert_called_once_with("notepad")
        mock_press.assert_called_once_with("enter")

