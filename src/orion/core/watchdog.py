"""The Autonomous Watchdog (Proactive Background Monitoring Engine) (Step 24).

Periodically evaluates system health (battery, thermals, and load) and dispatches
state-debounced, proactive alerts to the owner's Telegram chat.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time
from typing import Any
import psutil

logger = logging.getLogger("orion.core.watchdog")


@dataclass
class WatchdogState:
    """State of the autonomous watchdog."""

    last_battery_state: str = "normal"  # "normal", "low_20", "critical_10", "full_100"
    last_power_plugged: bool | None = None
    high_load_consecutive_cycles: int = 0
    last_thermal_alert_time: float = 0.0
    enabled: bool = True


class WatchdogEngine:
    """Autonomous proactive background vitals monitoring engine."""

    def __init__(
        self,
        bot_app: Any = None,
        owner_id: int = 0,
        check_interval_sec: int = 30,
    ) -> None:
        self.bot_app = bot_app
        self.owner_id = owner_id
        self.check_interval_sec = check_interval_sec
        self.state = WatchdogState()
        self._stopped = False

    async def check_vitals(self) -> list[str]:
        """Inspect system vitals (battery, thermals, load) and return pending alerts."""
        if not self.state.enabled:
            return []

        alerts: list[str] = []

        # 1. Battery check
        try:
            battery = psutil.sensors_battery()
            if battery is not None:
                percent = round(battery.percent)
                power_plugged = bool(battery.power_plugged)

                if not power_plugged:
                    # Discharging
                    if percent <= 10 and self.state.last_battery_state != "critical_10":
                        self.state.last_battery_state = "critical_10"
                        alerts.append(f"🚨 *Critical Battery:* Laptop is at {percent}% and discharging.")
                    elif 10 < percent <= 20 and self.state.last_battery_state not in ("low_20", "critical_10"):
                        self.state.last_battery_state = "low_20"
                        alerts.append(
                            f"⚠️ *Battery Alert:* Laptop is at {percent}% and discharging. Please connect charger."
                        )
                    elif percent < 95 and self.state.last_battery_state == "full_100":
                        self.state.last_battery_state = "normal"
                else:
                    # Charging / Plugged in
                    if self.state.last_battery_state in ("low_20", "critical_10") or (
                        self.state.last_power_plugged is False and self.state.last_battery_state != "normal"
                    ):
                        self.state.last_battery_state = "normal"
                        alerts.append(f"⚡ *Power Connected:* Laptop is now charging ({percent}%).")

                    if percent >= 99 and self.state.last_battery_state != "full_100":
                        self.state.last_battery_state = "full_100"
                        alerts.append(
                            "🔋 *Battery Fully Charged:* Laptop is at 100%. You can safely unplug the charger."
                        )
                    elif percent < 95 and self.state.last_battery_state == "full_100":
                        self.state.last_battery_state = "normal"

                self.state.last_power_plugged = power_plugged
        except Exception as e:
            logger.debug("Error checking battery sensors in watchdog: %s", e)

        # 2. Thermal / Heavy Load check
        try:
            cpu = psutil.cpu_percent(interval=None)
            gpu_temp: float | None = None
            try:
                from orion.tools.system import get_gpu_status

                gpu_status = get_gpu_status()
                if gpu_status.get("available") and gpu_status.get("temperature") is not None:
                    gpu_temp = float(gpu_status["temperature"])
            except Exception:
                gpu_temp = None

            is_high_load = cpu > 90.0 or (gpu_temp is not None and gpu_temp > 85.0)
            if is_high_load:
                self.state.high_load_consecutive_cycles += 1
                now = time.time()
                if self.state.high_load_consecutive_cycles >= 2 and (
                    now - self.state.last_thermal_alert_time > 900.0
                ):
                    top_proc = "Unknown"
                    try:
                        from orion.tools.process_manager import get_top_resource_processes

                        top_procs = get_top_resource_processes(metric="cpu", limit=1)
                        if top_procs:
                            top_proc = top_procs[0].get("name", "Unknown")
                    except Exception:
                        pass

                    alerts.append(
                        f"🔥 *Thermal/Load Warning:* Laptop CPU at {cpu:.0f}% (Top process: *{top_proc}*)."
                    )
                    self.state.last_thermal_alert_time = now
            else:
                self.state.high_load_consecutive_cycles = 0
        except Exception as e:
            logger.debug("Error checking CPU/GPU load in watchdog: %s", e)

        return alerts

    async def _dispatch_alert(self, alert_md: str) -> None:
        """Deliver alert message to owner's Telegram chat."""
        if not self.bot_app or not getattr(self.bot_app, "bot", None) or not self.owner_id:
            logger.debug("No active bot_app or owner_id configured to deliver watchdog alert: %s", alert_md)
            return

        from orion.integrations.formatter import markdown_to_telegram_html

        formatted = markdown_to_telegram_html(alert_md)
        try:
            await self.bot_app.bot.send_message(
                chat_id=self.owner_id,
                text=formatted,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("Failed to dispatch watchdog alert to Telegram: %s", e)

    async def run_loop(self) -> None:
        """Continuous background inspection loop."""
        logger.info("WatchdogEngine loop started (interval: %ss)", self.check_interval_sec)
        while not self._stopped:
            try:
                if self.state.enabled:
                    alerts = await self.check_vitals()
                    for alert in alerts:
                        await self._dispatch_alert(alert)
            except asyncio.CancelledError:
                logger.info("WatchdogEngine loop cancelled.")
                break
            except Exception as e:
                logger.warning("Error in WatchdogEngine check_vitals loop: %s", e)

            try:
                await asyncio.sleep(self.check_interval_sec)
            except asyncio.CancelledError:
                logger.info("WatchdogEngine sleep cancelled.")
                break

    def pause(self) -> str:
        """Pause the watchdog monitoring."""
        self.state.enabled = False
        return "Autonomous Watchdog paused."

    def resume(self) -> str:
        """Resume the watchdog monitoring."""
        self.state.enabled = True
        return "Autonomous Watchdog resumed."

    def get_status(self) -> dict[str, Any]:
        """Return watchdog status dictionary."""
        return {
            "enabled": self.state.enabled,
            "check_interval_sec": self.check_interval_sec,
            "last_battery_state": self.state.last_battery_state,
            "last_power_plugged": self.state.last_power_plugged,
            "high_load_consecutive_cycles": self.state.high_load_consecutive_cycles,
            "last_thermal_alert_time": self.state.last_thermal_alert_time,
        }

    async def stop(self) -> None:
        """Signal engine to terminate background loop."""
        self._stopped = True


# Global default engine instance
_default_engine: WatchdogEngine | None = None


def get_watchdog_engine() -> WatchdogEngine:
    """Retrieve global default watchdog engine."""
    global _default_engine
    if _default_engine is None:
        _default_engine = WatchdogEngine()
    return _default_engine


def set_watchdog_engine(engine: WatchdogEngine | None) -> None:
    """Set global default watchdog engine."""
    global _default_engine
    _default_engine = engine
