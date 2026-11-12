"""SQLite-backed persistent cron scheduler for ORION macros (Step 31)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Callable

from orion.config.settings import get_settings
from orion.core.macro_store import MacroStore, get_macro_store

logger = logging.getLogger("orion.core.macro_scheduler")


def _parse_time_of_day(raw_time: str) -> str:
    """Parse 12-hour or 24-hour time string into standard 24-hour 'HH:MM' string."""
    t = raw_time.strip().lower()

    # 1. 12-hour format with am/pm: e.g. "9:30 am", "9:30am", "2 pm", "2pm", "12am", "12pm"
    m_12 = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)$", t)
    if m_12:
        hour = int(m_12.group(1))
        minute = int(m_12.group(2)) if m_12.group(2) else 0
        meridiem = m_12.group(3)
        if hour < 1 or hour > 12 or minute < 0 or minute > 59:
            raise ValueError(f"Invalid time format: '{raw_time}'")
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        return f"{hour:02d}:{minute:02d}"

    # 2. 24-hour format: e.g. "09:00", "9:00", "14:30", "23:59"
    m_24 = re.match(r"^(\d{1,2}):(\d{2})$", t)
    if m_24:
        hour = int(m_24.group(1))
        minute = int(m_24.group(2))
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            raise ValueError(f"Invalid time format: '{raw_time}'")
        return f"{hour:02d}:{minute:02d}"

    raise ValueError(f"Could not parse time of day: '{raw_time}'. Expected 'HH:MM' (24-hour) or 'H:MM am/pm'.")


def human_readable_spec(schedule_type: str, time_of_day: str | None, interval_value: int | None) -> str:
    """Format schedule type and parameters into a clean human-readable description."""
    if schedule_type == "daily":
        return f"Daily at {time_of_day}"
    elif schedule_type == "weekdays":
        return f"Weekdays at {time_of_day}"
    elif schedule_type == "every_hours":
        val = interval_value or 1
        return f"Every {val} hours" if val > 1 else "Every hour"
    elif schedule_type == "every_minutes":
        val = interval_value or 1
        return f"Every {val} minutes" if val > 1 else "Every minute"
    return f"{schedule_type}"


class MacroScheduler:
    """Persistent scheduler for triggering saved macros on time-based schedules."""

    def __init__(
        self,
        db_path: Path | str | None = None,
        macro_store: MacroStore | None = None,
    ) -> None:
        if db_path is not None:
            self.db_path = Path(db_path)
        else:
            settings = get_settings()
            self.db_path = Path(settings.db_path) if getattr(settings, "db_path", None) else Path("data/orion.db")

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.macro_store = macro_store or get_macro_store(db_path=self.db_path)
        self._lock = threading.Lock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Create orion_macro_schedules table and indexes if not exists."""
        with self._lock:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS orion_macro_schedules (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        macro_name TEXT NOT NULL,
                        schedule_type TEXT NOT NULL,
                        time_of_day TEXT,
                        interval_value INTEGER,
                        enabled INTEGER DEFAULT 1,
                        last_run_at TEXT,
                        next_run_at TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_macro_schedules_next_run ON orion_macro_schedules(next_run_at);"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_macro_schedules_macro_name ON orion_macro_schedules(macro_name);"
                )

    def parse_schedule_spec(self, spec: str) -> dict[str, Any]:
        """Parse a natural schedule specification string into structured parameters.

        Supported specs:
        - "daily at 09:00", "every day at 9:30 am", "everyday at 2pm"
        - "weekdays at 08:30", "mon-fri at 8:30", "monday-friday at 9am"
        - "every 2 hours", "every hour", "every 4h"
        - "every 30 minutes", "every 15 mins", "every minute", "every 45m"
        """
        raw = spec.strip()
        clean = raw.lower()

        # 1. Weekdays: e.g. "weekdays at 08:30", "mon-fri at 8:30", "monday-friday at 9am"
        m_weekdays = re.match(
            r"^(?:on\s+)?(?:weekdays|mon(?:day)?\s*-\s*fri(?:day)?)\s+at\s+(.+)$",
            clean,
        )
        if m_weekdays:
            time_str = _parse_time_of_day(m_weekdays.group(1))
            return {
                "schedule_type": "weekdays",
                "time_of_day": time_str,
                "interval_value": None,
            }

        # 2. Daily: e.g. "daily at 09:00", "every day at 9:30 am", "everyday at 2pm"
        m_daily = re.match(r"^(?:daily|every\s*day)\s+at\s+(.+)$", clean)
        if m_daily:
            time_str = _parse_time_of_day(m_daily.group(1))
            return {
                "schedule_type": "daily",
                "time_of_day": time_str,
                "interval_value": None,
            }

        # 3. Every N hours: e.g. "every 2 hours", "every 1 hour", "every hour", "every 4h"
        m_every_hours = re.match(r"^every\s+(?:(\d+)\s*(?:hours?|h)|hour)$", clean)
        if m_every_hours:
            val = int(m_every_hours.group(1)) if m_every_hours.group(1) else 1
            if val <= 0:
                raise ValueError("Interval hours must be greater than 0.")
            return {
                "schedule_type": "every_hours",
                "time_of_day": None,
                "interval_value": val,
            }

        # 4. Every N minutes: e.g. "every 30 minutes", "every 15 mins", "every minute", "every 45m"
        m_every_mins = re.match(r"^every\s+(?:(\d+)\s*(?:minutes?|mins?|m)|minute)$", clean)
        if m_every_mins:
            val = int(m_every_mins.group(1)) if m_every_mins.group(1) else 1
            if val <= 0:
                raise ValueError("Interval minutes must be greater than 0.")
            return {
                "schedule_type": "every_minutes",
                "time_of_day": None,
                "interval_value": val,
            }

        raise ValueError(
            f"Invalid schedule specification: '{spec}'. "
            "Supported formats: 'daily at HH:MM', 'weekdays at HH:MM', 'every N hours', 'every N minutes'."
        )

    def compute_next_run(
        self,
        schedule_type: str,
        time_of_day: str | None,
        interval_value: int | None,
        now: datetime | None = None,
    ) -> datetime:
        """Compute the next local trigger timestamp based on schedule parameters."""
        if now is None:
            now = datetime.now()
        now = now.replace(microsecond=0)

        if schedule_type == "every_minutes":
            interval = interval_value or 1
            return now + timedelta(minutes=interval)

        elif schedule_type == "every_hours":
            interval = interval_value or 1
            return now + timedelta(hours=interval)

        elif schedule_type == "daily":
            if not time_of_day:
                raise ValueError("time_of_day is required for daily schedule.")
            hour, minute = (int(x) for x in time_of_day.split(":"))
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            return target

        elif schedule_type == "weekdays":
            if not time_of_day:
                raise ValueError("time_of_day is required for weekdays schedule.")
            hour, minute = (int(x) for x in time_of_day.split(":"))
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            # Advance past Saturday (5) and Sunday (6)
            while target.weekday() >= 5:
                target += timedelta(days=1)
            return target

        else:
            raise ValueError(f"Unknown schedule type: '{schedule_type}'")

    def add_schedule(self, macro_name: str, spec: str) -> dict[str, Any]:
        """Add a persistent scheduled trigger for an existing macro."""
        clean_name = macro_name.strip().lower()
        macro = self.macro_store.get_macro(clean_name)
        if not macro:
            raise ValueError(f"Macro '{clean_name}' does not exist.")

        parsed = self.parse_schedule_spec(spec)
        next_run = self.compute_next_run(
            schedule_type=parsed["schedule_type"],
            time_of_day=parsed["time_of_day"],
            interval_value=parsed["interval_value"],
        )

        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO orion_macro_schedules
                    (macro_name, schedule_type, time_of_day, interval_value, enabled, last_run_at, next_run_at)
                    VALUES (?, ?, ?, ?, 1, NULL, ?)
                    """,
                    (
                        clean_name,
                        parsed["schedule_type"],
                        parsed["time_of_day"],
                        parsed["interval_value"],
                        next_run.isoformat(),
                    ),
                )
                row_id = cursor.lastrowid

        return self.get_schedule(row_id) or {
            "id": row_id,
            "macro_name": clean_name,
            "schedule_type": parsed["schedule_type"],
            "time_of_day": parsed["time_of_day"],
            "interval_value": parsed["interval_value"],
            "enabled": 1,
            "last_run_at": None,
            "next_run_at": next_run.isoformat(),
        }

    def list_schedules(self) -> list[dict[str, Any]]:
        """List all macro schedules ordered by ID."""
        with self._lock:
            with self._get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT id, macro_name, schedule_type, time_of_day, interval_value, enabled, last_run_at, next_run_at, created_at
                    FROM orion_macro_schedules
                    ORDER BY id ASC
                    """
                ).fetchall()
                return [dict(r) for r in rows]

    def get_schedule(self, schedule_id: int) -> dict[str, Any] | None:
        """Retrieve a specific schedule by ID."""
        with self._lock:
            with self._get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT id, macro_name, schedule_type, time_of_day, interval_value, enabled, last_run_at, next_run_at, created_at
                    FROM orion_macro_schedules
                    WHERE id = ?
                    """,
                    (schedule_id,),
                ).fetchone()
                return dict(row) if row else None

    def delete_schedule(self, schedule_id: int) -> bool:
        """Delete a schedule by ID. Returns True if deleted, False otherwise."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    "DELETE FROM orion_macro_schedules WHERE id = ?",
                    (schedule_id,),
                )
                return cursor.rowcount > 0

    def get_due_schedules(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Retrieve all enabled schedules where next_run_at <= now."""
        if now is None:
            now = datetime.now()
        now_dt = now.replace(microsecond=0)

        with self._lock:
            with self._get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT id, macro_name, schedule_type, time_of_day, interval_value, enabled, last_run_at, next_run_at, created_at
                    FROM orion_macro_schedules
                    WHERE enabled = 1
                    ORDER BY id ASC
                    """
                ).fetchall()

        due = []
        for r in rows:
            d = dict(r)
            try:
                run_dt = datetime.fromisoformat(d["next_run_at"])
                if run_dt <= now_dt:
                    due.append(d)
            except Exception as e:
                logger.warning("Could not parse next_run_at for schedule #%s: %s", d.get("id"), e)
        return due

    def mark_executed(self, schedule_id: int, now: datetime | None = None) -> None:
        """Mark schedule executed: record last_run_at and advance next_run_at."""
        if now is None:
            now = datetime.now()
        now_dt = now.replace(microsecond=0)

        sched = self.get_schedule(schedule_id)
        if not sched:
            return

        next_run = self.compute_next_run(
            schedule_type=sched["schedule_type"],
            time_of_day=sched["time_of_day"],
            interval_value=sched["interval_value"],
            now=now_dt,
        )

        with self._lock:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    UPDATE orion_macro_schedules
                    SET last_run_at = ?, next_run_at = ?
                    WHERE id = ?
                    """,
                    (now_dt.isoformat(), next_run.isoformat(), schedule_id),
                )

    def format_schedule_list(self, schedules: list[dict[str, Any]]) -> str:
        """Format schedule list into pure Telegram Markdown."""
        if not schedules:
            return "📅 *No scheduled macros configured.*\nUse `schedule macro <name> <spec>` to add one."

        lines = [f"📅 *Scheduled Macros ({len(schedules)}):*"]
        for s in schedules:
            sched_id = s.get("id")
            m_name = s.get("macro_name", "")
            stype = s.get("schedule_type", "")
            t_day = s.get("time_of_day")
            ival = s.get("interval_value")
            desc = human_readable_spec(stype, t_day, ival)

            next_run = s.get("next_run_at", "")
            try:
                dt = datetime.fromisoformat(next_run)
                next_run_str = dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                next_run_str = next_run

            last_run = s.get("last_run_at")
            if last_run:
                try:
                    dt_last = datetime.fromisoformat(last_run)
                    last_run_str = dt_last.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    last_run_str = last_run
            else:
                last_run_str = "Never"

            lines.append(f"\n• *#{sched_id}* `{m_name}` — {desc}")
            lines.append(f"  • Next run: `{next_run_str}`")
            lines.append(f"  • Last run: `{last_run_str}`")

        return "\n".join(lines)


async def check_and_execute_due_schedules(
    scheduler: MacroScheduler,
    macro_store: MacroStore,
    deliver_fn: Callable[[str], Any],
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Check for due macro schedules, execute their workflows, mark executed, and deliver reports."""
    from orion.core.workflow_runner import execute_workflow
    from orion.tools.router import route_and_execute_tool

    executed = []
    due_list = scheduler.get_due_schedules(now=now)
    for sched in due_list:
        m_name = sched["macro_name"]
        macro = macro_store.get_macro(m_name)
        # Advance next_run_at first so a slow execution or transient error doesn't loop
        scheduler.mark_executed(sched["id"], now=now)

        if not macro or not macro.get("steps"):
            fail_report = f"⚠️ *Scheduled Macro '{m_name}' Failed:* Macro no longer exists or contains no steps."
            await deliver_fn(fail_report)
            executed.append({"schedule": sched, "success": False, "reply": fail_report})
            continue

        res = await execute_workflow(
            macro["steps"],
            route_and_execute_tool,
            context={"_in_workflow": True, "_scheduled": True},
        )
        report_body = res.get("reply", "Executed successfully.")
        report = f"⏰ *Scheduled Macro '{m_name}':*\n{report_body}"
        await deliver_fn(report)
        executed.append({"schedule": sched, "success": res.get("success", True), "reply": report})

    return executed


async def run_macro_scheduler_loop(
    scheduler: MacroScheduler,
    macro_store: MacroStore,
    deliver_fn: Callable[[str], Any],
    poll_interval_sec: float = 30.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Async background polling loop for continuously evaluating and executing scheduled macros."""
    while stop_event is None or not stop_event.is_set():
        try:
            await check_and_execute_due_schedules(
                scheduler=scheduler,
                macro_store=macro_store,
                deliver_fn=deliver_fn,
            )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Error in macro scheduler background loop: %s", e)

        try:
            if stop_event:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_sec)
                break
            else:
                await asyncio.sleep(poll_interval_sec)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            break


_macro_scheduler: MacroScheduler | None = None


def get_macro_scheduler(
    db_path: Path | str | None = None,
    macro_store: MacroStore | None = None,
) -> MacroScheduler:
    """Retrieve or create the singleton MacroScheduler instance."""
    global _macro_scheduler
    if _macro_scheduler is None or db_path is not None:
        _macro_scheduler = MacroScheduler(db_path=db_path, macro_store=macro_store)
    return _macro_scheduler
