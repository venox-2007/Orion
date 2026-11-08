"""Unit tests for Step 24 — The Autonomous Watchdog (Proactive Background Monitoring Engine)."""

import asyncio
from collections import namedtuple
import re
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from orion.core.watchdog import (
    WatchdogEngine,
    WatchdogState,
    get_watchdog_engine,
    set_watchdog_engine,
)
from orion.tools.router import (
    check_deterministic_watchdog_query,
    route_and_execute_tool,
)

BatteryInfo = namedtuple("BatteryInfo", ["percent", "secsleft", "power_plugged"])


@pytest.mark.asyncio
async def test_battery_low_and_critical_alerts():
    """Verify low and critical battery alerts trigger and debounce duplicates."""
    watchdog = WatchdogEngine()

    # 1. 18% unplugged -> Low battery alert
    b_18 = BatteryInfo(percent=18, secsleft=3600, power_plugged=False)
    with patch("psutil.sensors_battery", return_value=b_18), \
         patch("psutil.cpu_percent", return_value=15.0), \
         patch("orion.tools.system.get_gpu_status", return_value={"available": False}):
        alerts = await watchdog.check_vitals()
        assert len(alerts) == 1
        assert "⚠️ *Battery Alert:* Laptop is at 18% and discharging" in alerts[0]
        assert watchdog.state.last_battery_state == "low_20"

        # 2. Second check at 18% -> Debounced, no duplicate alert
        alerts2 = await watchdog.check_vitals()
        assert len(alerts2) == 0
        assert watchdog.state.last_battery_state == "low_20"

    # 3. 8% unplugged -> Critical battery alert
    b_8 = BatteryInfo(percent=8, secsleft=1200, power_plugged=False)
    with patch("psutil.sensors_battery", return_value=b_8), \
         patch("psutil.cpu_percent", return_value=15.0), \
         patch("orion.tools.system.get_gpu_status", return_value={"available": False}):
        alerts3 = await watchdog.check_vitals()
        assert len(alerts3) == 1
        assert "🚨 *Critical Battery:* Laptop is at 8% and discharging." in alerts3[0]
        assert watchdog.state.last_battery_state == "critical_10"

        # 4. Another check at 8% -> Debounced
        alerts4 = await watchdog.check_vitals()
        assert len(alerts4) == 0


@pytest.mark.asyncio
async def test_battery_plugged_in_reset():
    """Verify charger plug-in notifies owner, resets state, and notifies on 100% full charge."""
    watchdog = WatchdogEngine()
    watchdog.state.last_battery_state = "critical_10"
    watchdog.state.last_power_plugged = False

    # 1. Plug in charger at 12%
    b_charging = BatteryInfo(percent=12, secsleft=-1, power_plugged=True)
    with patch("psutil.sensors_battery", return_value=b_charging), \
         patch("psutil.cpu_percent", return_value=10.0), \
         patch("orion.tools.system.get_gpu_status", return_value={"available": False}):
        alerts = await watchdog.check_vitals()
        assert len(alerts) == 1
        assert "⚡ *Power Connected:* Laptop is now charging (12%)." in alerts[0]
        assert watchdog.state.last_battery_state == "normal"
        assert watchdog.state.last_power_plugged is True

        # Check again at 15% plugged in -> No duplicate charging alert
        b_charging15 = BatteryInfo(percent=15, secsleft=-1, power_plugged=True)
        with patch("psutil.sensors_battery", return_value=b_charging15):
            alerts_subsequent = await watchdog.check_vitals()
            assert len(alerts_subsequent) == 0

    # 2. Battery reaches 100% plugged in -> Full charge alert
    b_full = BatteryInfo(percent=100, secsleft=-1, power_plugged=True)
    with patch("psutil.sensors_battery", return_value=b_full), \
         patch("psutil.cpu_percent", return_value=10.0), \
         patch("orion.tools.system.get_gpu_status", return_value={"available": False}):
        alerts_full = await watchdog.check_vitals()
        assert len(alerts_full) == 1
        assert "🔋 *Battery Fully Charged:* Laptop is at 100%." in alerts_full[0]
        assert watchdog.state.last_battery_state == "full_100"

        # Debounced on repeat
        alerts_full2 = await watchdog.check_vitals()
        assert len(alerts_full2) == 0


@pytest.mark.asyncio
async def test_thermal_consecutive_cycles():
    """Verify thermal/load spikes require 2 consecutive cycles before alerting, and respect cooldown."""
    watchdog = WatchdogEngine()
    watchdog.state.last_thermal_alert_time = 0.0

    mock_proc = [{"name": "Blender", "cpu_percent": 92.0}]

    with patch("psutil.sensors_battery", return_value=None), \
         patch("orion.tools.system.get_gpu_status", return_value={"available": False}), \
         patch("orion.tools.process_manager.get_top_resource_processes", return_value=mock_proc):

        # Cycle 1: CPU at 95% -> Spike detected, but no alert yet (cycle count = 1)
        with patch("psutil.cpu_percent", return_value=95.0):
            alerts_c1 = await watchdog.check_vitals()
            assert len(alerts_c1) == 0
            assert watchdog.state.high_load_consecutive_cycles == 1

        # Cycle 2: CPU still at 95% -> Consecutive cycle threshold reached (>=2) -> Alert triggered
        with patch("psutil.cpu_percent", return_value=95.0):
            alerts_c2 = await watchdog.check_vitals()
            assert len(alerts_c2) == 1
            assert "🔥 *Thermal/Load Warning:*" in alerts_c2[0]
            assert "95%" in alerts_c2[0]
            assert "Blender" in alerts_c2[0]
            assert watchdog.state.high_load_consecutive_cycles == 2

        # Cycle 3: CPU still high but within 15-minute cooldown -> No duplicate alert
        with patch("psutil.cpu_percent", return_value=95.0):
            alerts_c3 = await watchdog.check_vitals()
            assert len(alerts_c3) == 0
            assert watchdog.state.high_load_consecutive_cycles == 3

        # Cycle 4: CPU drops to normal (25%) -> Consecutive count resets to 0
        with patch("psutil.cpu_percent", return_value=25.0):
            alerts_c4 = await watchdog.check_vitals()
            assert len(alerts_c4) == 0
            assert watchdog.state.high_load_consecutive_cycles == 0


def test_watchdog_pause_resume():
    """Verify pause, resume, and get_status toggle watchdog state cleanly."""
    watchdog = WatchdogEngine(check_interval_sec=15)
    assert watchdog.state.enabled is True

    # 1. Pause
    msg_pause = watchdog.pause()
    assert watchdog.state.enabled is False
    assert "paused" in msg_pause.lower()

    # Status reflects paused
    status = watchdog.get_status()
    assert status["enabled"] is False
    assert status["check_interval_sec"] == 15

    # 2. Resume
    msg_resume = watchdog.resume()
    assert watchdog.state.enabled is True
    assert "resumed" in msg_resume.lower()

    # Status reflects enabled
    status2 = watchdog.get_status()
    assert status2["enabled"] is True


@pytest.mark.asyncio
async def test_router_watchdog_queries():
    """Verify router handles watchdog status, pause, and resume queries deterministically with zero raw HTML."""
    watchdog = WatchdogEngine(check_interval_sec=30)
    set_watchdog_engine(watchdog)

    # 1. Watchdog status (active)
    res_status = await route_and_execute_tool("watchdog status")
    assert res_status is not None
    assert res_status.get("handled") is True
    assert "🛡️ *Watchdog Status:* Active" in res_status.get("reply", "")
    assert "• Interval: 30s" in res_status.get("reply", "")
    assert re.search(r"</?(?:b|i|s|code|pre)>", res_status["reply"]) is None

    # Alternative status phrases
    res_alt1 = await route_and_execute_tool("status of watchdog")
    assert res_alt1 is not None and res_alt1.get("handled") is True
    res_alt2 = await route_and_execute_tool("is watchdog running?")
    assert res_alt2 is not None and res_alt2.get("handled") is True

    # 2. Pause watchdog
    res_pause = await route_and_execute_tool("pause watchdog")
    assert res_pause is not None
    assert res_pause.get("handled") is True
    assert "⏸️ *Autonomous Watchdog paused.*" in res_pause.get("reply", "")
    assert watchdog.state.enabled is False
    assert re.search(r"</?(?:b|i|s|code|pre)>", res_pause["reply"]) is None

    # Status when paused
    res_status_paused = await route_and_execute_tool("watchdog status")
    assert res_status_paused is not None
    assert "🛡️ *Watchdog Status:* Paused" in res_status_paused.get("reply", "")

    # 3. Resume watchdog
    res_resume = await route_and_execute_tool("resume watchdog")
    assert res_resume is not None
    assert res_resume.get("handled") is True
    assert "▶️ *Autonomous Watchdog resumed.*" in res_resume.get("reply", "")
    assert watchdog.state.enabled is True
    assert re.search(r"</?(?:b|i|s|code|pre)>", res_resume["reply"]) is None

    # 4. Alternative controls: "disable watchdog", "enable watchdog", "stop watchdog", "start watchdog"
    res_stop = await route_and_execute_tool("stop watchdog")
    assert res_stop is not None and res_stop.get("handled") is True
    assert watchdog.state.enabled is False

    res_start = await route_and_execute_tool("start watchdog")
    assert res_start is not None and res_start.get("handled") is True
    assert watchdog.state.enabled is True


@pytest.mark.asyncio
async def test_watchdog_dispatch_and_loop_cancellation():
    """Verify run_loop dispatches alert to Telegram via HTML conversion and handles cancellation."""
    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()
    mock_app = MagicMock()
    mock_app.bot = mock_bot

    watchdog = WatchdogEngine(bot_app=mock_app, owner_id=99999, check_interval_sec=1)

    b_low = BatteryInfo(percent=15, secsleft=2000, power_plugged=False)
    with patch("psutil.sensors_battery", return_value=b_low), \
         patch("psutil.cpu_percent", return_value=10.0), \
         patch("orion.tools.system.get_gpu_status", return_value={"available": False}):

        loop_task = asyncio.create_task(watchdog.run_loop())
        # Give event loop a cycle to run one iteration
        await asyncio.sleep(0.05)

        # Alert should have dispatched
        assert mock_bot.send_message.called
        call_kwargs = mock_bot.send_message.call_args.kwargs
        assert call_kwargs["chat_id"] == 99999
        assert call_kwargs["parse_mode"] == "HTML"
        assert "<i>Battery Alert:</i>" in call_kwargs["text"]

        # Cancel cleanly
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

        assert loop_task.done()
