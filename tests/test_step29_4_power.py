"""Unit tests for Step 29.4 — Unified Power & Session Management."""

import re
from unittest.mock import call, patch
import pytest

from orion.tools.router import (
    check_deterministic_process_and_power_query,
    route_and_execute_tool,
)
from orion.tools.workstation import (
    cancel_shutdown,
    hibernate_system,
    lock_workstation,
    shutdown_system,
    sleep_system,
    turn_off_screen,
)


def test_workstation_power_functions_mocked():
    """Verify low-level workstation power APIs invoke expected Windows and subprocess calls."""
    # 1. Lock Workstation
    with patch("ctypes.windll.user32.LockWorkStation", return_value=1) as mock_lock:
        assert lock_workstation() is True
        mock_lock.assert_called_once()

    # 2. Turn off Screen
    with patch("ctypes.windll.user32.SendMessageW", return_value=0) as mock_msg:
        assert turn_off_screen() is True
        mock_msg.assert_called_once_with(0xFFFF, 0x0112, 0xF170, 2)

    # 3. Sleep System
    with patch("ctypes.windll.PowrProf.SetSuspendState", return_value=1) as mock_suspend:
        assert sleep_system() is True
        mock_suspend.assert_called_once_with(0, 1, 0)

    # 4. Hibernate System
    with patch("ctypes.windll.PowrProf.SetSuspendState", return_value=1) as mock_hib:
        assert hibernate_system() is True
        mock_hib.assert_called_once_with(1, 1, 0)

    # 5. Shutdown System (Timed)
    with patch("subprocess.run") as mock_subproc:
        assert shutdown_system(restart=False, delay_sec=10) is True
        mock_subproc.assert_called_once_with(["shutdown.exe", "/s", "/t", "10"], check=True)

    # 6. Restart System (Timed)
    with patch("subprocess.run") as mock_subproc:
        assert shutdown_system(restart=True, delay_sec=10) is True
        mock_subproc.assert_called_once_with(["shutdown.exe", "/r", "/t", "10"], check=True)

    # 7. Cancel Shutdown
    with patch("subprocess.run") as mock_subproc:
        assert cancel_shutdown() is True
        mock_subproc.assert_called_once_with(["shutdown.exe", "/a"], check=True)


@pytest.mark.asyncio
async def test_power_router_sleep_variants():
    """Verify all sleep phrasing variants route deterministically and invoke sleep_system."""
    sleep_queries = [
        "put the laptop to sleep",
        "sleep",
        "sleep laptop",
        "put my pc to sleep",
        "go to sleep",
        "suspend system",
    ]
    for q in sleep_queries:
        with patch("orion.tools.workstation.sleep_system", return_value=True) as mock_sleep:
            res = await route_and_execute_tool(q)
            assert res is not None, f"Query '{q}' returned None"
            assert res.get("handled") is True
            mock_sleep.assert_called_once()
            reply = res.get("reply", "")
            assert "Putting laptop to sleep." in reply
            assert re.search(r"</?(?:b|i|s|code|pre)>", reply) is None


@pytest.mark.asyncio
async def test_power_router_screen_off_variants():
    """Verify display / screen off phrasing variants route deterministically."""
    screen_queries = [
        "turn off the screen",
        "screen off",
        "turn monitor off",
        "turn off display",
        "display off",
        "turn my screen off",
    ]
    for q in screen_queries:
        with patch("orion.tools.workstation.turn_off_screen", return_value=True) as mock_screen:
            res = await route_and_execute_tool(q)
            assert res is not None, f"Query '{q}' returned None"
            assert res.get("handled") is True
            mock_screen.assert_called_once()
            reply = res.get("reply", "")
            assert "Display turned off." in reply
            assert re.search(r"</?(?:b|i|s|code|pre)>", reply) is None


@pytest.mark.asyncio
async def test_power_router_lock_variants():
    """Verify lock phrasing variants route deterministically and return clean replies."""
    lock_queries = [
        "lock",
        "lock the pc",
        "lock the laptop",
        "lock down",
        "lock it",
    ]
    for q in lock_queries:
        with patch("orion.tools.workstation.lock_workstation", return_value=True) as mock_lock:
            res = await route_and_execute_tool(q)
            assert res is not None, f"Query '{q}' returned None"
            assert res.get("handled") is True
            mock_lock.assert_called_once()
            reply = res.get("reply", "")
            assert "Workstation locked." in reply
            assert re.search(r"</?(?:b|i|s|code|pre)>", reply) is None


@pytest.mark.asyncio
async def test_power_router_hibernate_variants():
    """Verify hibernate phrasing variants route deterministically."""
    hib_queries = [
        "hibernate",
        "hibernate laptop",
        "hibernate the pc",
        "put the laptop to hibernate",
        "put my computer to hibernate",
    ]
    for q in hib_queries:
        with patch("orion.tools.workstation.hibernate_system", return_value=True) as mock_hib:
            res = await route_and_execute_tool(q)
            assert res is not None, f"Query '{q}' returned None"
            assert res.get("handled") is True
            mock_hib.assert_called_once()
            reply = res.get("reply", "")
            assert "Hibernating system." in reply
            assert re.search(r"</?(?:b|i|s|code|pre)>", reply) is None


@pytest.mark.asyncio
async def test_power_router_shutdown_and_restart():
    """Verify timed shutdown, restart, and abort queries execute safely."""
    # 1. Shutdown
    with patch("orion.tools.workstation.shutdown_system", return_value=True) as mock_shut:
        res1 = await route_and_execute_tool("shut down")
        assert res1 is not None and res1.get("handled") is True
        mock_shut.assert_called_once_with(restart=False, delay_sec=10)
        assert "Shutdown initiated (10s delay)" in res1.get("reply", "")
        assert re.search(r"</?(?:b|i|s|code|pre)>", res1["reply"]) is None

        # Suffix variant
        mock_shut.reset_mock()
        res1_suffix = await route_and_execute_tool("shutdown the laptop")
        assert res1_suffix is not None and res1_suffix.get("handled") is True
        mock_shut.assert_called_once_with(restart=False, delay_sec=10)

    # 2. Restart
    with patch("orion.tools.workstation.shutdown_system", return_value=True) as mock_restart:
        res2 = await route_and_execute_tool("restart")
        assert res2 is not None and res2.get("handled") is True
        mock_restart.assert_called_once_with(restart=True, delay_sec=10)
        assert "Restart initiated (10s delay)" in res2.get("reply", "")
        assert re.search(r"</?(?:b|i|s|code|pre)>", res2["reply"]) is None

        # Reboot variant
        mock_restart.reset_mock()
        res2_reboot = await route_and_execute_tool("reboot pc")
        assert res2_reboot is not None and res2_reboot.get("handled") is True
        mock_restart.assert_called_once_with(restart=True, delay_sec=10)

    # 3. Abort / Cancel
    with patch("orion.tools.workstation.cancel_shutdown", return_value=True) as mock_cancel:
        res3 = await route_and_execute_tool("cancel shutdown")
        assert res3 is not None and res3.get("handled") is True
        mock_cancel.assert_called_once()
        assert "Shutdown / Restart canceled." in res3.get("reply", "")
        assert re.search(r"</?(?:b|i|s|code|pre)>", res3["reply"]) is None

        mock_cancel.reset_mock()
        res3_abort = await route_and_execute_tool("abort restart")
        assert res3_abort is not None and res3_abort.get("handled") is True
        mock_cancel.assert_called_once()
