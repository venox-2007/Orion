"""Unit tests for Step 31 — Scheduled Cron Macros (Time-Based Autonomous Workflow Triggers)."""

from datetime import datetime, timedelta
from pathlib import Path
import re
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from orion.core.macro_scheduler import (
    MacroScheduler,
    check_and_execute_due_schedules,
    human_readable_spec,
    _parse_time_of_day,
)
from orion.core.macro_store import MacroStore
from orion.tools.router import route_and_execute_tool


@pytest.fixture
def temp_macro_env(tmp_path: Path):
    """Create isolated SQLite database with MacroStore and MacroScheduler."""
    db_file = tmp_path / "test_orion.db"
    store = MacroStore(db_path=db_file)
    scheduler = MacroScheduler(db_path=db_file, macro_store=store)
    return store, scheduler, db_file


def test_time_of_day_parsing():
    """Verify 12h and 24h string time parsing into 24h 'HH:MM' format."""
    # 12-hour format
    assert _parse_time_of_day("9:30 am") == "09:30"
    assert _parse_time_of_day("9:30am") == "09:30"
    assert _parse_time_of_day("2 pm") == "14:00"
    assert _parse_time_of_day("2pm") == "14:00"
    assert _parse_time_of_day("12 pm") == "12:00"
    assert _parse_time_of_day("12 am") == "00:00"
    assert _parse_time_of_day("11:45 pm") == "23:45"
    assert _parse_time_of_day("12:15 am") == "00:15"

    # 24-hour format
    assert _parse_time_of_day("09:00") == "09:00"
    assert _parse_time_of_day("9:00") == "09:00"
    assert _parse_time_of_day("14:30") == "14:30"
    assert _parse_time_of_day("00:00") == "00:00"
    assert _parse_time_of_day("23:59") == "23:59"

    # Invalid formats
    with pytest.raises(ValueError):
        _parse_time_of_day("25:00")
    with pytest.raises(ValueError):
        _parse_time_of_day("13:00 pm")
    with pytest.raises(ValueError):
        _parse_time_of_day("invalid time")


def test_parse_schedule_spec(temp_macro_env):
    """Verify natural schedule specifications are correctly parsed."""
    _, scheduler, _ = temp_macro_env

    # 1. Daily
    spec_daily1 = scheduler.parse_schedule_spec("daily at 09:00")
    assert spec_daily1 == {"schedule_type": "daily", "time_of_day": "09:00", "interval_value": None}

    spec_daily2 = scheduler.parse_schedule_spec("every day at 9:30 am")
    assert spec_daily2 == {"schedule_type": "daily", "time_of_day": "09:30", "interval_value": None}

    spec_daily3 = scheduler.parse_schedule_spec("everyday at 2pm")
    assert spec_daily3 == {"schedule_type": "daily", "time_of_day": "14:00", "interval_value": None}

    # 2. Weekdays
    spec_wk1 = scheduler.parse_schedule_spec("weekdays at 08:30")
    assert spec_wk1 == {"schedule_type": "weekdays", "time_of_day": "08:30", "interval_value": None}

    spec_wk2 = scheduler.parse_schedule_spec("mon-fri at 8:30")
    assert spec_wk2 == {"schedule_type": "weekdays", "time_of_day": "08:30", "interval_value": None}

    spec_wk3 = scheduler.parse_schedule_spec("monday-friday at 9am")
    assert spec_wk3 == {"schedule_type": "weekdays", "time_of_day": "09:00", "interval_value": None}

    # 3. Every N hours
    spec_h1 = scheduler.parse_schedule_spec("every 2 hours")
    assert spec_h1 == {"schedule_type": "every_hours", "time_of_day": None, "interval_value": 2}

    spec_h2 = scheduler.parse_schedule_spec("every hour")
    assert spec_h2 == {"schedule_type": "every_hours", "time_of_day": None, "interval_value": 1}

    # 4. Every N minutes
    spec_m1 = scheduler.parse_schedule_spec("every 30 minutes")
    assert spec_m1 == {"schedule_type": "every_minutes", "time_of_day": None, "interval_value": 30}

    spec_m2 = scheduler.parse_schedule_spec("every 15 mins")
    assert spec_m2 == {"schedule_type": "every_minutes", "time_of_day": None, "interval_value": 15}

    spec_m3 = scheduler.parse_schedule_spec("every minute")
    assert spec_m3 == {"schedule_type": "every_minutes", "time_of_day": None, "interval_value": 1}

    # Invalid spec
    with pytest.raises(ValueError):
        scheduler.parse_schedule_spec("yearly on christmas")


def test_compute_next_run(temp_macro_env):
    """Verify next trigger calculation for daily, weekdays (skipping weekends), and interval types."""
    _, scheduler, _ = temp_macro_env

    # 1. Daily before target time
    now_thurs_morning = datetime(2026, 11, 12, 8, 0, 0)  # Thursday 08:00
    next_daily = scheduler.compute_next_run("daily", "09:00", None, now=now_thurs_morning)
    assert next_daily == datetime(2026, 11, 12, 9, 0, 0)

    # 2. Daily after target time (rolls to tomorrow)
    now_thurs_afternoon = datetime(2026, 11, 12, 14, 0, 0)
    next_daily_rollover = scheduler.compute_next_run("daily", "09:00", None, now=now_thurs_afternoon)
    assert next_daily_rollover == datetime(2026, 11, 13, 9, 0, 0)

    # 3. Weekdays on Friday afternoon (rolls to Monday, skipping Sat and Sun)
    now_fri_afternoon = datetime(2026, 11, 13, 17, 0, 0)  # Friday 17:00
    next_wk_from_fri = scheduler.compute_next_run("weekdays", "08:30", None, now=now_fri_afternoon)
    assert next_wk_from_fri == datetime(2026, 11, 16, 8, 30, 0)  # Monday 08:30
    assert next_wk_from_fri.weekday() == 0  # 0 is Monday

    # 4. Weekdays evaluated on a Saturday (skips Sunday, sets to Monday)
    now_sat = datetime(2026, 11, 14, 10, 0, 0)  # Saturday 10:00
    next_wk_from_sat = scheduler.compute_next_run("weekdays", "08:30", None, now=now_sat)
    assert next_wk_from_sat == datetime(2026, 11, 16, 8, 30, 0)  # Monday 08:30

    # 5. Every hours
    next_2h = scheduler.compute_next_run("every_hours", None, 2, now=now_thurs_morning)
    assert next_2h == datetime(2026, 11, 12, 10, 0, 0)

    # 6. Every minutes
    next_30m = scheduler.compute_next_run("every_minutes", None, 30, now=now_thurs_morning)
    assert next_30m == datetime(2026, 11, 12, 8, 30, 0)


def test_scheduler_crud_lifecycle(temp_macro_env):
    """Verify add, list, get_due, mark_executed, and delete operations."""
    store, scheduler, _ = temp_macro_env

    # 1. Attempt to schedule non-existent macro raises ValueError
    with pytest.raises(ValueError, match="does not exist"):
        scheduler.add_schedule("unregistered_macro", "daily at 09:00")

    # 2. Save macro and add schedule
    store.save_macro("dev", ["ps: git status", "where is my mouse"], description="Dev check")
    sched = scheduler.add_schedule("dev", "every 2 hours")
    assert sched["id"] == 1
    assert sched["macro_name"] == "dev"
    assert sched["schedule_type"] == "every_hours"
    assert sched["interval_value"] == 2
    assert sched["last_run_at"] is None
    assert sched["next_run_at"] is not None

    # 3. Add second schedule
    store.save_macro("morning", ["open notepad", "type hello in notepad"])
    sched2 = scheduler.add_schedule("morning", "daily at 08:30")
    assert sched2["id"] == 2

    # 4. List schedules
    schedules = scheduler.list_schedules()
    assert len(schedules) == 2
    assert schedules[0]["macro_name"] == "dev"
    assert schedules[1]["macro_name"] == "morning"

    # 5. Format schedule list (pure Markdown, no raw HTML tags)
    formatted = scheduler.format_schedule_list(schedules)
    assert "📅 *Scheduled Macros (2):*" in formatted
    assert "• *#1* `dev` — Every 2 hours" in formatted
    assert "• *#2* `morning` — Daily at 08:30" in formatted
    assert re.search(r"</?(?:b|i|s|code|pre)>", formatted) is None

    # 6. Due schedules: set current time after next_run_at
    future_time = datetime.now() + timedelta(hours=3)
    due = scheduler.get_due_schedules(now=future_time)
    assert len(due) >= 1
    dev_due = [s for s in due if s["macro_name"] == "dev"]
    assert len(dev_due) == 1

    # 7. Mark executed: advances next_run_at and sets last_run_at
    old_next_run = dev_due[0]["next_run_at"]
    scheduler.mark_executed(dev_due[0]["id"], now=future_time)

    updated_dev = scheduler.get_schedule(dev_due[0]["id"])
    assert updated_dev["last_run_at"] is not None
    assert updated_dev["next_run_at"] > old_next_run

    # 8. Delete schedule
    assert scheduler.delete_schedule(sched["id"]) is True
    assert scheduler.get_schedule(sched["id"]) is None
    assert scheduler.delete_schedule(999) is False


@pytest.mark.asyncio
async def test_check_and_execute_due_schedules(temp_macro_env):
    """Verify background evaluation triggers macro workflows and pushes execution reports."""
    store, scheduler, _ = temp_macro_env

    # Save test macro
    store.save_macro("cleanup", ["type test in notepad", "where is my mouse"])

    # Create schedule due in the past
    sched = scheduler.add_schedule("cleanup", "every 1 hour")

    # Mock past time
    mock_now = datetime.now() + timedelta(hours=2)
    delivered_reports = []

    async def mock_deliver(report: str):
        delivered_reports.append(report)

    with patch("orion.core.workflow_runner.execute_workflow", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = {
            "handled": True,
            "success": True,
            "reply": "1. ✅ *type test in notepad*\n2. ✅ *where is my mouse*",
        }

        results = await check_and_execute_due_schedules(
            scheduler=scheduler,
            macro_store=store,
            deliver_fn=mock_deliver,
            now=mock_now,
        )

        assert len(results) == 1
        assert results[0]["success"] is True
        assert len(delivered_reports) == 1
        assert "⏰ *Scheduled Macro 'cleanup':*" in delivered_reports[0]
        assert "1. ✅ *type test in notepad*" in delivered_reports[0]
        assert re.search(r"</?(?:b|i|s|code|pre)>", delivered_reports[0]) is None

        # Verify next_run_at was advanced and schedule is no longer due at mock_now
        due_again = scheduler.get_due_schedules(now=mock_now)
        assert len(due_again) == 0


@pytest.mark.asyncio
async def test_router_schedule_commands(temp_macro_env):
    """Verify router deterministically parses 'schedule macro', 'list schedules', and 'delete schedule'."""
    store, scheduler, _ = temp_macro_env

    with patch("orion.core.macro_scheduler.get_macro_scheduler", return_value=scheduler), \
         patch("orion.core.macro_store.get_macro_store", return_value=store):

        # 1. Schedule macro that doesn't exist
        res_missing = await route_and_execute_tool("schedule macro unknown_task daily at 09:00")
        assert res_missing is not None
        assert res_missing["handled"] is True
        assert "⚠️ Macro '*unknown_task*' does not exist." in res_missing["reply"]

        # 2. Save macro and schedule it
        store.save_macro("backup", ["ps: git status", "where is my mouse"])

        res_sched = await route_and_execute_tool("schedule macro backup daily at 08:30")
        assert res_sched is not None
        assert res_sched["handled"] is True
        assert "⏰ *Macro 'backup' scheduled:*" in res_sched["reply"]
        assert "Daily at 08:30" in res_sched["reply"]
        assert "ID: `#1`" in res_sched["reply"]
        assert re.search(r"</?(?:b|i|s|code|pre)>", res_sched["reply"]) is None

        # 3. Schedule with interval
        res_sched2 = await route_and_execute_tool("schedule macro backup every 2 hours")
        assert res_sched2 is not None
        assert res_sched2["handled"] is True
        assert "⏰ *Macro 'backup' scheduled:*" in res_sched2["reply"]
        assert "Every 2 hours" in res_sched2["reply"]

        # 4. Invalid schedule specification
        res_invalid = await route_and_execute_tool("schedule macro backup whenever it feels right")
        assert res_invalid is not None
        assert res_invalid["handled"] is True
        assert "⚠️ Invalid schedule format:" in res_invalid["reply"]

        # 5. List schedules
        res_list = await route_and_execute_tool("list schedules")
        assert res_list is not None
        assert res_list["handled"] is True
        assert "📅 *Scheduled Macros (2):*" in res_list["reply"]
        assert "Daily at 08:30" in res_list["reply"]
        assert "Every 2 hours" in res_list["reply"]

        # Sched list alias: "schedules"
        res_list_alias = await route_and_execute_tool("schedules")
        assert res_list_alias is not None
        assert res_list_alias["handled"] is True
        assert "📅 *Scheduled Macros (2):*" in res_list_alias["reply"]

        # 6. Delete schedule
        res_del = await route_and_execute_tool("delete schedule 1")
        assert res_del is not None
        assert res_del["handled"] is True
        assert "🗑️ *Scheduled trigger #1 deleted.*" in res_del["reply"]

        # Delete with '#' prefix
        res_del2 = await route_and_execute_tool("cancel schedule #2")
        assert res_del2 is not None
        assert res_del2["handled"] is True
        assert "🗑️ *Scheduled trigger #2 deleted.*" in res_del2["reply"]

        # Delete non-existent schedule
        res_del_missing = await route_and_execute_tool("remove schedule 99")
        assert res_del_missing is not None
        assert res_del_missing["handled"] is True
        assert "⚠️ Schedule trigger `#99` not found." in res_del_missing["reply"]

        # Empty schedule list
        res_empty = await route_and_execute_tool("list schedules")
        assert "📅 *No scheduled macros configured.*" in res_empty["reply"]
