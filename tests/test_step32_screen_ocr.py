"""Unit tests for Step 32 — Native Windows Screen OCR Engine & OCR Click Fallback."""

import re
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from orion.tools import screen_ocr, ui_locator
from orion.tools.router import route_and_execute_tool


def test_bounding_box_offset_math():
    """Verify offset_x and offset_y correctly translate relative coordinates to absolute screen space."""
    mock_raw_ocr = {
        "success": True,
        "text": "File Edit View",
        "lines": [
            {
                "text": "File Edit View",
                "words": [
                    {"text": "File", "x": 10, "y": 5, "w": 30, "h": 20},
                    {"text": "Edit", "x": 50, "y": 5, "w": 30, "h": 20},
                    {"text": "View", "x": 90, "y": 5, "w": 30, "h": 20},
                ],
            }
        ],
        "words": [
            {"text": "File", "x": 10, "y": 5, "w": 30, "h": 20},
            {"text": "Edit", "x": 50, "y": 5, "w": 30, "h": 20},
            {"text": "View", "x": 90, "y": 5, "w": 30, "h": 20},
        ],
    }

    # Simulate captured window offset at (200, 150)
    with patch("orion.tools.screen_ocr.capture_target_region") as mock_cap, \
         patch("orion.tools.screen_ocr._run_winrt_ocr_ps", return_value=mock_raw_ocr):
        
        mock_cap.return_value = (MagicMock(exists=lambda: False), (200, 150))
        result = screen_ocr.extract_screen_text_sync(target_app="notepad")

        assert result["success"] is True
        words = result["words"]
        assert len(words) == 3

        # Word 1: File
        assert words[0]["x"] == 210  # 10 + 200
        assert words[0]["y"] == 155  # 5 + 150
        assert words[0]["center_x"] == 225  # 210 + 30 // 2
        assert words[0]["center_y"] == 165  # 155 + 20 // 2

        # Word 2: Edit
        assert words[1]["x"] == 250  # 50 + 200
        assert words[1]["y"] == 155  # 5 + 150
        assert words[1]["center_x"] == 265  # 250 + 30 // 2
        assert words[1]["center_y"] == 165  # 155 + 20 // 2


def test_find_text_coordinates_single_and_multiword():
    """Verify single-word and multi-word phrase matching across OCR lines."""
    mock_ocr_data = {
        "success": True,
        "text": "Sign In to Google\nYouTube Bookmark Manager",
        "lines": [
            {
                "text": "Sign In to Google",
                "words": [
                    {"text": "Sign", "x": 100, "y": 50, "w": 40, "h": 20, "center_x": 120, "center_y": 60},
                    {"text": "In", "x": 145, "y": 50, "w": 20, "h": 20, "center_x": 155, "center_y": 60},
                    {"text": "to", "x": 170, "y": 50, "w": 20, "h": 20, "center_x": 180, "center_y": 60},
                    {"text": "Google", "x": 195, "y": 50, "w": 60, "h": 20, "center_x": 225, "center_y": 60},
                ],
            },
            {
                "text": "YouTube Bookmark Manager",
                "words": [
                    {"text": "YouTube", "x": 50, "y": 120, "w": 80, "h": 25, "center_x": 90, "center_y": 132},
                    {"text": "Bookmark", "x": 135, "y": 120, "w": 90, "h": 25, "center_x": 180, "center_y": 132},
                    {"text": "Manager", "x": 230, "y": 120, "w": 80, "h": 25, "center_x": 270, "center_y": 132},
                ],
            },
        ],
        "words": [
            {"text": "Sign", "x": 100, "y": 50, "w": 40, "h": 20, "center_x": 120, "center_y": 60},
            {"text": "In", "x": 145, "y": 50, "w": 20, "h": 20, "center_x": 155, "center_y": 60},
            {"text": "to", "x": 170, "y": 50, "w": 20, "h": 20, "center_x": 180, "center_y": 60},
            {"text": "Google", "x": 195, "y": 50, "w": 60, "h": 20, "center_x": 225, "center_y": 60},
            {"text": "YouTube", "x": 50, "y": 120, "w": 80, "h": 25, "center_x": 90, "center_y": 132},
            {"text": "Bookmark", "x": 135, "y": 120, "w": 90, "h": 25, "center_x": 180, "center_y": 132},
            {"text": "Manager", "x": 230, "y": 120, "w": 80, "h": 25, "center_x": 270, "center_y": 132},
        ],
    }

    with patch("orion.tools.screen_ocr.extract_screen_text_sync", return_value=mock_ocr_data):
        # 1. Exact single word: "Google"
        coords_google = screen_ocr.find_text_coordinates_sync("Google")
        assert coords_google == (225, 60)

        # 2. Case-insensitive single word: "sign"
        coords_sign = screen_ocr.find_text_coordinates_sync("sign")
        assert coords_sign == (120, 60)

        # 3. Multi-word phrase: "Sign In"
        # Spans "Sign" (x:100..140) and "In" (x:145..165) -> min_x=100, max_x=165 -> center_x=132, center_y=60
        coords_signin = screen_ocr.find_text_coordinates_sync("Sign In")
        assert coords_signin is not None
        assert coords_signin[0] == (100 + 165) // 2
        assert coords_signin[1] == 60

        # 4. Multi-word phrase: "youtube bookmark"
        # Spans "YouTube" (x:50..130) and "Bookmark" (x:135..225) -> min_x=50, max_x=225 -> center_x=137, center_y=132
        coords_yt = screen_ocr.find_text_coordinates_sync("youtube bookmark")
        assert coords_yt is not None
        assert coords_yt[0] == (50 + 225) // 2
        assert coords_yt[1] == 132

        # 5. Non-existent query
        coords_missing = screen_ocr.find_text_coordinates_sync("Settings Option")
        assert coords_missing is None


def test_ui_locator_ocr_fallback():
    """Verify click_element_by_name falls back to OCR when UIAutomation finds no match."""
    # 1. UIAutomation finds nothing (None), but OCR locates coordinates
    with patch("orion.tools.ui_locator.find_element_coordinates", return_value=None), \
         patch("orion.tools.ui_locator._find_uia_control", return_value=None), \
         patch("orion.tools.screen_ocr.find_text_coordinates_sync", return_value=(450, 320)) as mock_ocr, \
         patch("orion.tools.mouse_control.mouse_click") as mock_click:

        success, reply = ui_locator.click_element_by_name("Canvas Element")
        assert success is True
        assert "👁️ OCR-located and clicked *Canvas Element* at (450, 320)." in reply
        mock_ocr.assert_called_once_with("Canvas Element", target_app=None)
        mock_click.assert_called_once_with(450, 320, button="left", clicks=1)
        assert re.search(r"</?(?:b|i|s|code|pre)>", reply) is None

    # 2. Both UIAutomation and OCR fail to locate element
    with patch("orion.tools.input_control.focus_window_by_name", return_value=True), \
         patch("orion.tools.ui_locator.find_element_coordinates", return_value=None), \
         patch("orion.tools.ui_locator._find_uia_control", return_value=None), \
         patch("orion.tools.screen_ocr.find_text_coordinates_sync", return_value=None):

        success_fail, reply_fail = ui_locator.click_element_by_name("Phantom Button", target_app="chrome")
        assert success_fail is False
        assert "⚠️ Could not locate UI element *Phantom Button* in *chrome*." in reply_fail


@pytest.mark.asyncio
async def test_router_ocr_commands():
    """Verify router handles OCR text extraction commands deterministically."""
    mock_ocr_result = {
        "success": True,
        "text": "System Status: Online\nAll services operational.",
        "lines": [],
        "words": [],
    }

    with patch("orion.tools.screen_ocr.extract_screen_text", new_callable=AsyncMock) as mock_extract:
        mock_extract.return_value = mock_ocr_result

        # 1. "read screen"
        res1 = await route_and_execute_tool("read screen")
        assert res1 is not None
        assert res1["handled"] is True
        assert "👁️ *Screen OCR (Active Screen):*" in res1["reply"]
        assert "System Status: Online" in res1["reply"]
        mock_extract.assert_called_with(target_app=None)
        assert re.search(r"</?(?:b|i|s|code|pre)>", res1["reply"]) is None

        # 2. "ocr screen in notepad"
        res2 = await route_and_execute_tool("ocr screen in notepad")
        assert res2 is not None
        assert res2["handled"] is True
        assert "👁️ *Screen OCR (notepad):*" in res2["reply"]
        mock_extract.assert_called_with(target_app="notepad")

        # 3. "read text from chrome"
        res3 = await route_and_execute_tool("read text from chrome")
        assert res3 is not None
        assert res3["handled"] is True
        assert "👁️ *Screen OCR (chrome):*" in res3["reply"]

        # 4. "extract text"
        res4 = await route_and_execute_tool("extract text")
        assert res4 is not None
        assert res4["handled"] is True
        assert "👁️ *Screen OCR (Active Screen):*" in res4["reply"]

    # 5. Empty OCR result handling
    with patch("orion.tools.screen_ocr.extract_screen_text", new_callable=AsyncMock) as mock_empty:
        mock_empty.return_value = {"success": True, "text": "", "lines": [], "words": []}
        res_empty = await route_and_execute_tool("read the screen")
        assert res_empty is not None
        assert res_empty["handled"] is True
        assert "_No text detected on screen._" in res_empty["reply"]

    # 6. Long text truncation handling
    long_text = "Word " * 800  # 4000 characters
    with patch("orion.tools.screen_ocr.extract_screen_text", new_callable=AsyncMock) as mock_long:
        mock_long.return_value = {"success": True, "text": long_text, "lines": [], "words": []}
        res_trunc = await route_and_execute_tool("ocr")
        assert res_trunc is not None
        assert res_trunc["handled"] is True
        assert "... [truncated]" in res_trunc["reply"]
        assert len(res_trunc["reply"]) < 3200
