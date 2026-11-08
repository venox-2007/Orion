"""Executive briefing engine for ORION (Step 26)."""

from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path
from typing import Any
import psutil

from orion.core.memory_store import MemoryStore, get_memory_store
from orion.core.reminders import ReminderManager
from orion.core.tasks import TaskManager

logger = logging.getLogger("orion.core.briefing")


def generate_executive_briefing(db_path: Path | str | None = None) -> str:
    """Generate a deterministic executive daily briefing markdown report."""
    now = datetime.now()
    header_date = now.strftime("%A, %b %d")

    # 1. Header
    sections: list[str] = [f"🌅 *ORION Executive Briefing — {header_date}*"]

    # 2. System Vitals
    try:
        battery = psutil.sensors_battery()
        if battery is not None:
            pct = int(battery.percent)
            is_charging = battery.power_plugged
            icon = "⚡" if is_charging else "🔋"
            state = "Charging" if is_charging else "Discharging"
            battery_str = f"{icon} {pct}% ({state})"
        else:
            battery_str = "🔌 AC Power (No battery)"
    except Exception as e:
        logger.debug("Failed to read battery status for briefing: %s", e)
        battery_str = "Unavailable"

    try:
        cpu_pct = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory()
        ram_used = ram.used / (1024**3)
        ram_total = ram.total / (1024**3)
        cpu_ram_str = f"CPU: {cpu_pct:.0f}% | RAM: {ram_used:.1f}/{ram_total:.1f} GB"
    except Exception as e:
        logger.debug("Failed to read CPU/RAM for briefing: %s", e)
        cpu_ram_str = "CPU / RAM metrics unavailable"

    vitals_section = (
        "📊 *System Vitals:*\n"
        f"• Battery: {battery_str}\n"
        f"• {cpu_ram_str}"
    )
    sections.append(vitals_section)

    # 3. Pending Tasks
    try:
        task_mgr = TaskManager(db_path=db_path)
        pending_tasks = task_mgr.get_pending_tasks()
        if pending_tasks:
            total_tasks = len(pending_tasks)
            task_lines = [f"📋 *Pending Tasks ({total_tasks}):*"]
            for t in pending_tasks[:3]:
                task_lines.append(f"• {t['title']}")
            if total_tasks > 3:
                task_lines.append(f"_...and {total_tasks - 3} more_")
            sections.append("\n".join(task_lines))
        else:
            sections.append("📋 *Pending Tasks:* None")
    except Exception as e:
        logger.warning("Failed to retrieve tasks for briefing: %s", e)
        sections.append("📋 *Pending Tasks:* Unavailable")

    # 4. Upcoming Reminders (24h)
    try:
        reminder_mgr = ReminderManager(db_path=db_path)
        upcoming = reminder_mgr.get_upcoming_reminders(hours=24.0)
        if upcoming:
            rem_lines = [f"⏰ *Upcoming Reminders (24h):*"]
            for r in upcoming[:5]:
                dt = datetime.fromtimestamp(r["scheduled_at"])
                time_str = dt.strftime("%I:%M %p")
                rem_lines.append(f"• {time_str} — {r['message']}")
            if len(upcoming) > 5:
                rem_lines.append(f"_...and {len(upcoming) - 5} more_")
            sections.append("\n".join(rem_lines))
        else:
            sections.append("⏰ *Upcoming Reminders:* None")
    except Exception as e:
        logger.warning("Failed to retrieve reminders for briefing: %s", e)
        sections.append("⏰ *Upcoming Reminders:* Unavailable")

    # 5. Pinned / Active Memories
    try:
        mem_store = get_memory_store(db_path=db_path)
        memories = mem_store.get_all_memories(limit=2)
        if memories:
            mem_lines = ["🧠 *Pinned Memories:*"]
            for m in memories:
                mem_lines.append(f"• *{m['key']}*: {m['fact']}")
            sections.append("\n".join(mem_lines))
        else:
            sections.append("🧠 *Pinned Memories:* None")
    except Exception as e:
        logger.warning("Failed to retrieve memories for briefing: %s", e)
        sections.append("🧠 *Pinned Memories:* None")

    return "\n\n".join(sections)
