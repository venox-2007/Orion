"""Tests for Step 35 — Spatial Window Tiling & Workspace Presets."""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from orion.tools import window_tiler
from orion.tools.router import route_and_execute_tool


def test_calculate_tile_geometry():
    """Verify geometry calculation for left, right, top-left, bottom-right, and center."""
    work_area = (0, 0, 1920, 1000)

    # Left 50%
    geo_left = window_tiler.calculate_tile_geometry("left", work_area=work_area)
    assert geo_left == (0, 0, 960, 1000)

    # Right 50%
    geo_right = window_tiler.calculate_tile_geometry("right", work_area=work_area)
    assert geo_right == (960, 0, 960, 1000)

    # Top-Left quadrant
    geo_tl = window_tiler.calculate_tile_geometry("top-left", work_area=work_area)
    assert geo_tl == (0, 0, 960, 500)

    # Bottom-Right quadrant
    geo_br = window_tiler.calculate_tile_geometry("bottom-right", work_area=work_area)
    assert geo_br == (960, 500, 960, 500)

    # Center (70% width, 80% height)
    # W = 1920, w = int(1920 * 0.7) = 1344, x = (1920 - 1344) // 2 = 288
    # H = 1000, h = int(1000 * 0.8) = 800, y = (1000 - 800) // 2 = 100
    geo_center = window_tiler.calculate_tile_geometry("center", work_area=work_area)
    assert geo_center == (288, 100, 1344, 800)

    # Maximize returns None (signals ShowWindow SW_MAXIMIZE)
    geo_max = window_tiler.calculate_tile_geometry("maximize", work_area=work_area)
    assert geo_max is None


def test_tile_window_positioning():
    """Verify tile_window restores window and invokes SetWindowPos with correct geometry."""
    mock_hwnd = 12345
    work_area = (0, 0, 1920, 1080)

    with patch("orion.tools.window_tiler.find_target_window", return_value=(mock_hwnd, "Google Chrome")), \
         patch("orion.tools.window_tiler.get_work_area", return_value=work_area), \
         patch("orion.tools.input_control.force_foreground_window", return_value=True), \
         patch("ctypes.windll.user32.IsIconic", return_value=True), \
         patch("ctypes.windll.user32.ShowWindow") as mock_show_win, \
         patch("ctypes.windll.user32.SetWindowPos") as mock_set_pos:

        # Tile Left
        success, msg = window_tiler.tile_window("chrome", "left")

        assert success is True
        assert "Snapped *Google Chrome* to the *left*." in msg
        # Restores window first (SW_RESTORE = 9)
        mock_show_win.assert_called_once_with(mock_hwnd, 9)
        # Sets position (hwnd, HWND_TOP=0, x=0, y=0, w=960, h=1080, SWP_SHOWWINDOW=0x0040)
        mock_set_pos.assert_called_once_with(mock_hwnd, 0, 0, 0, 960, 1080, 0x0040)
        assert "<" not in msg and ">" not in msg


def test_tile_window_maximize():
    """Verify tile_window maximize invokes ShowWindow with SW_MAXIMIZE = 3."""
    mock_hwnd = 54321

    with patch("orion.tools.window_tiler.find_target_window", return_value=(mock_hwnd, "Visual Studio Code")), \
         patch("orion.tools.input_control.force_foreground_window", return_value=True), \
         patch("ctypes.windll.user32.IsIconic", return_value=False), \
         patch("ctypes.windll.user32.IsZoomed", return_value=False), \
         patch("ctypes.windll.user32.ShowWindow") as mock_show_win:

        success, msg = window_tiler.tile_window("code", "maximize")

        assert success is True
        assert "Maximized *Visual Studio Code* to full screen." in msg
        mock_show_win.assert_called_once_with(mock_hwnd, 3)


@pytest.mark.asyncio
async def test_apply_workspace_preset_coding():
    """Verify apply_workspace_preset('coding') arranges VS Code left and Chrome right."""
    with patch("orion.tools.window_tiler.find_target_window") as mock_find, \
         patch("orion.tools.window_tiler.tile_window") as mock_tile:

        # Both apps already open
        mock_find.side_effect = lambda app: (111 if "code" in app else 222, app.title())
        mock_tile.return_value = (True, "Tiled successfully.")

        success, msg = await window_tiler.apply_workspace_preset("coding")

        assert success is True
        assert "Workspace Preset 'coding' Applied:" in msg
        assert "VS Code" in msg and "left" in msg
        assert "Google Chrome" in msg and "right" in msg
        assert "<" not in msg and ">" not in msg


@pytest.mark.asyncio
async def test_router_tiling_commands():
    """Verify router handles snap, split, and workspace presets deterministically."""
    # 1. Snap Chrome Left
    with patch("orion.tools.window_tiler.tile_window", return_value=(True, "🪟 Snapped *Google Chrome* to the *left*.")) as mock_tile:
        res_snap = await route_and_execute_tool("snap chrome left")
        assert res_snap is not None
        assert res_snap["handled"] is True
        assert "Snapped *Google Chrome* to the *left*." in res_snap["reply"]
        mock_tile.assert_called_once_with("chrome", "left")

    # 2. Side-by-Side Split
    with patch("orion.tools.window_tiler.tile_window", return_value=(True, "OK")):
        res_split = await route_and_execute_tool("tile code and notepad")
        assert res_split is not None
        assert res_split["handled"] is True
        assert "Side-by-Side Split Applied:" in res_split["reply"]
        assert "*code* snapped to *left*" in res_split["reply"]
        assert "*notepad* snapped to *right*" in res_split["reply"]

    # 3. Workspace Presets
    with patch("orion.tools.window_tiler.apply_workspace_preset", new=AsyncMock(return_value=(True, "🪟 *Workspace Preset 'coding' Applied:*"))) as mock_preset:
        res_preset = await route_and_execute_tool("coding mode")
        assert res_preset is not None
        assert res_preset["handled"] is True
        assert "Workspace Preset 'coding' Applied:" in res_preset["reply"]
        mock_preset.assert_called_once_with("coding")

    # 4. Research Workspace
    with patch("orion.tools.window_tiler.apply_workspace_preset", new=AsyncMock(return_value=(True, "🪟 *Workspace Preset 'research' Applied:*"))) as mock_research:
        res_research = await route_and_execute_tool("switch to research workspace")
        assert res_research is not None
        assert res_research["handled"] is True
        assert "Workspace Preset 'research' Applied:" in res_research["reply"]
        mock_research.assert_called_once_with("research")
