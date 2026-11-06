"""Tests for Step 17: Local Personal Reminders in ORION."""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import time
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from orion.config.settings import Settings
from orion.core.reminders import (
    ReminderScheduler,
    calculate_next_occurrence,
    format_reminder_list,
    handle_reminder_text,
    parse_reminder_request,
)
from orion.integrations.telegram_bot import create_telegram_app
from orion.memory.database import ConversationMemory


@pytest.fixture
def temp_memory(tmp_path: Path) -> ConversationMemory:
    """Fixture providing a fresh isolated SQLite database."""
    db_file = tmp_path / "test_reminders.db"
    return ConversationMemory(db_path=str(db_file))


# ===========================================================================
# 1. ONE-TIME REMINDER CREATION & PARSING
# ===========================================================================

def test_parse_one_time_reminder_same_day():
    """'Remind me to study at 8 PM' parses target time and message cleanly."""
    tz = ZoneInfo("Asia/Kolkata")
    now_dt = datetime(2026, 11, 6, 14, 0, tzinfo=tz)  # 2:00 PM
    res = parse_reminder_request("Remind me to study at 8 PM", now_dt=now_dt, tz_name="Asia/Kolkata")

    assert res.is_reminder_intent is True
    assert res.needs_clarification is False
    assert res.message == "study"
    assert res.recurrence is None
    assert res.target_time_display == "today at 8:00 PM"

    dt = datetime.fromtimestamp(res.scheduled_at, tz=tz)
    assert dt.hour == 20
    assert dt.minute == 0
    assert dt.day == 6


def test_parse_one_time_reminder_already_past_today_advances_to_tomorrow():
    """'Remind me to study at 8 AM' when it is 2 PM advances to tomorrow."""
    tz = ZoneInfo("Asia/Kolkata")
    now_dt = datetime(2026, 11, 6, 14, 0, tzinfo=tz)  # 2:00 PM
    res = parse_reminder_request("Remind me to study at 8 AM", now_dt=now_dt, tz_name="Asia/Kolkata")

    assert res.is_reminder_intent is True
    assert res.message == "study"
    assert res.target_time_display == "tomorrow at 8:00 AM"

    dt = datetime.fromtimestamp(res.scheduled_at, tz=tz)
    assert dt.hour == 8
    assert dt.day == 7


def test_parse_one_time_reminder_tomorrow_at_10_am():
    """'Remind me tomorrow at 10 AM to call mom' parses reversed order."""
    tz = ZoneInfo("Asia/Kolkata")
    now_dt = datetime(2026, 11, 6, 14, 0, tzinfo=tz)
    res = parse_reminder_request(
        "Remind me tomorrow at 10 AM to call mom",
        now_dt=now_dt,
        tz_name="Asia/Kolkata",
    )

    assert res.is_reminder_intent is True
    assert res.message == "call mom"
    assert res.recurrence is None
    assert res.target_time_display == "tomorrow at 10:00 AM"

    dt = datetime.fromtimestamp(res.scheduled_at, tz=tz)
    assert dt.hour == 10
    assert dt.day == 7


def test_parse_relative_time_reminder():
    """'Remind me in 15 minutes to take a break' calculates delta correctly."""
    tz = ZoneInfo("Asia/Kolkata")
    now_dt = datetime(2026, 11, 6, 14, 0, tzinfo=tz)
    res = parse_reminder_request(
        "Remind me in 15 minutes to take a break",
        now_dt=now_dt,
        tz_name="Asia/Kolkata",
    )

    assert res.is_reminder_intent is True
    assert res.message == "take a break"
    assert "in 15 minutes" in res.target_time_display

    dt = datetime.fromtimestamp(res.scheduled_at, tz=tz)
    assert dt.minute == 15


# ===========================================================================
# 2. RECURRING REMINDER SCHEDULING
# ===========================================================================

def test_parse_daily_recurring_reminder():
    """'Remind me every day at 7 AM to wake up' sets daily recurrence."""
    tz = ZoneInfo("Asia/Kolkata")
    now_dt = datetime(2026, 11, 6, 14, 0, tzinfo=tz)  # 2:00 PM
    res = parse_reminder_request(
        "Remind me every day at 7 AM to wake up",
        now_dt=now_dt,
        tz_name="Asia/Kolkata",
    )

    assert res.is_reminder_intent is True
    assert res.message == "wake up"
    assert res.recurrence == "daily"
    assert "every day at 7:00 AM" in res.target_time_display

    dt = datetime.fromtimestamp(res.scheduled_at, tz=tz)
    assert dt.hour == 7
    # Since 7 AM today has passed, first occurrence is tomorrow
    assert dt.day == 7


def test_parse_weekly_recurring_reminder():
    """'Remind me every Monday at 6 PM to go to gym' sets weekly recurrence."""
    tz = ZoneInfo("Asia/Kolkata")
    now_dt = datetime(2026, 11, 6, 14, 0, tzinfo=tz)  # Friday Nov 6
    res = parse_reminder_request(
        "Remind me every Monday at 6 PM to go to gym",
        now_dt=now_dt,
        tz_name="Asia/Kolkata",
    )

    assert res.is_reminder_intent is True
    assert res.message == "go to gym"
    assert res.recurrence == "weekly:monday"
    assert "every Monday at 6:00 PM" in res.target_time_display

    dt = datetime.fromtimestamp(res.scheduled_at, tz=tz)
    assert dt.weekday() == 0  # Monday
    assert dt.hour == 18
    assert dt.day == 9  # Nov 9


def test_calculate_next_occurrence_daily():
    """Next occurrence calculation for daily advances by 24h into the future."""
    tz = ZoneInfo("Asia/Kolkata")
    sched_dt = datetime(2026, 11, 6, 7, 0, tzinfo=tz)
    reminder = {
        "scheduled_at": sched_dt.timestamp(),
        "recurrence": "daily",
    }

    # Delivered at 7:00 AM on Nov 6
    delivered_now = sched_dt.timestamp()
    next_ts = calculate_next_occurrence(reminder, now=delivered_now, tz_name="Asia/Kolkata")
    assert next_ts is not None

    next_dt = datetime.fromtimestamp(next_ts, tz=tz)
    assert next_dt.day == 7
    assert next_dt.hour == 7
    assert next_dt.minute == 0


def test_calculate_next_occurrence_weekly():
    """Next occurrence calculation for weekly advances by 7 days into future."""
    tz = ZoneInfo("Asia/Kolkata")
    sched_dt = datetime(2026, 11, 9, 18, 0, tzinfo=tz)  # Monday Nov 9
    reminder = {
        "scheduled_at": sched_dt.timestamp(),
        "recurrence": "weekly:monday",
    }

    delivered_now = sched_dt.timestamp()
    next_ts = calculate_next_occurrence(reminder, now=delivered_now, tz_name="Asia/Kolkata")
    assert next_ts is not None

    next_dt = datetime.fromtimestamp(next_ts, tz=tz)
    assert next_dt.day == 16  # Next Monday Nov 16
    assert next_dt.hour == 18


# ===========================================================================
# 3. CLARIFICATION HANDLING & FACTUAL QUERY EXCLUSION
# ===========================================================================

def test_missing_time_asks_for_clarification():
    """'Remind me to study' without time asks when to remind."""
    res = parse_reminder_request("Remind me to study")
    assert res.is_reminder_intent is True
    assert res.needs_clarification is True
    assert "When would you like me to remind you to study?" in res.clarification_prompt


def test_missing_message_asks_for_clarification():
    """'Remind me at 8 PM' without message asks what to remind."""
    res = parse_reminder_request("Remind me at 8 PM")
    assert res.is_reminder_intent is True
    assert res.needs_clarification is True
    assert "What would you like me to remind you about" in res.clarification_prompt


def test_factual_remind_me_questions_are_not_hijacked():
    """'Remind me who the president of France is' flows to conversation, not scheduler."""
    for phrase in [
        "Remind me who the president of France is",
        "Remind me what your name is",
        "Remind me how photosynthesis works",
    ]:
        res = parse_reminder_request(phrase)
        assert res.is_reminder_intent is False


# ===========================================================================
# 4. DATABASE PERSISTENCE, RESTART & OVERDUE RECOVERY
# ===========================================================================

def test_reminder_survives_bot_restart(temp_memory):
    """A reminder created in SQLite persists when ConversationMemory is reloaded."""
    owner_id = 999
    now_ts = datetime(2026, 11, 6, 20, 0, tzinfo=ZoneInfo("Asia/Kolkata")).timestamp()

    rid = temp_memory.create_reminder(
        user_id=owner_id,
        message="Deep Learning study session",
        scheduled_at=now_ts,
        recurrence="daily",
        timezone="Asia/Kolkata",
    )
    assert rid > 0

    # Simulate restart by instantiating new ConversationMemory with same db path
    restarted_memory = ConversationMemory(db_path=str(temp_memory.db_path))
    active = restarted_memory.get_active_reminders(owner_id)

    assert len(active) == 1
    assert active[0]["id"] == rid
    assert active[0]["message"] == "Deep Learning study session"
    assert active[0]["recurrence"] == "daily"


def test_overdue_reminders_recovered_after_restart(temp_memory):
    """Reminders that became due while bot was offline are detected upon restart."""
    owner_id = 999
    tz = ZoneInfo("Asia/Kolkata")
    past_time = (datetime(2026, 11, 6, 10, 0, tzinfo=tz) - timedelta(hours=2)).timestamp()

    rid = temp_memory.create_reminder(
        user_id=owner_id,
        message="Take morning vitamins",
        scheduled_at=past_time,
        recurrence=None,
    )

    restarted_memory = ConversationMemory(db_path=str(temp_memory.db_path))
    due = restarted_memory.get_due_reminders(now=past_time + 10)

    assert len(due) == 1
    assert due[0]["id"] == rid
    assert due[0]["message"] == "Take morning vitamins"


# ===========================================================================
# 5. REMINDER MANAGEMENT: LISTING & CANCELLATION
# ===========================================================================

def test_list_and_cancel_reminder_lifecycle(temp_memory):
    """Create, list, and cancel reminder via handle_reminder_text."""
    owner_id = 12345
    ctx = {"user_id": owner_id, "owner_id": owner_id, "memory": temp_memory}

    # 1. Create reminder
    create_res = handle_reminder_text("Remind me tomorrow at 10 AM to call mom", context=ctx)
    assert create_res is not None
    assert create_res["handled"] is True
    assert "call mom" in create_res["reply"]
    rid = create_res["reminder_id"]

    # 2. List reminders
    list_res = handle_reminder_text("Show my reminders", context=ctx)
    assert list_res is not None
    assert f"ID: {rid}" in list_res["reply"]
    assert "call mom" in list_res["reply"]

    # 3. Cancel reminder
    cancel_res = handle_reminder_text(f"Cancel reminder {rid}", context=ctx)
    assert cancel_res is not None
    assert "has been cancelled" in cancel_res["reply"]

    # 4. Verify no active reminders remain
    list_after = handle_reminder_text("Show my reminders", context=ctx)
    assert "no active reminders" in list_after["reply"].lower()


def test_cancel_nonexistent_reminder(temp_memory):
    """Cancelling a non-existent ID reports honest failure."""
    owner_id = 12345
    ctx = {"user_id": owner_id, "owner_id": owner_id, "memory": temp_memory}
    res = handle_reminder_text("Cancel reminder 999", context=ctx)
    assert res is not None
    assert "not found or is already cancelled" in res["reply"]


# ===========================================================================
# 6. SCHEDULER: DUE REMINDERS, DELIVERY & DUPLICATE PROTECTION
# ===========================================================================

@pytest.mark.asyncio
async def test_scheduler_delivers_due_reminder_cleanly(temp_memory):
    """Scheduler checks due reminders and invokes deliver callback with simple format."""
    owner_id = 777
    tz = ZoneInfo("Asia/Kolkata")
    sched_ts = datetime(2026, 11, 6, 20, 0, tzinfo=tz).timestamp()

    rid = temp_memory.create_reminder(
        user_id=owner_id,
        message="Study Deep Learning",
        scheduled_at=sched_ts,
    )

    delivered_messages = []

    async def mock_deliver(uid: int, text: str) -> None:
        delivered_messages.append((uid, text))

    scheduler = ReminderScheduler(
        memory=temp_memory,
        deliver_callback=mock_deliver,
        owner_id=owner_id,
        check_interval=1.0,
        timezone_name="Asia/Kolkata",
    )

    # 1. Before scheduled time -> 0 delivered
    count_before = await scheduler.check_due_reminders(now=sched_ts - 10)
    assert count_before == 0
    assert len(delivered_messages) == 0

    # 2. At / past scheduled time -> delivered
    count_due = await scheduler.check_due_reminders(now=sched_ts + 1)
    assert count_due == 1
    assert len(delivered_messages) == 1
    assert delivered_messages[0] == (owner_id, "Reminder: Study Deep Learning")

    # 3. Duplicate protection: checking again immediately does NOT deliver duplicate
    count_dup = await scheduler.check_due_reminders(now=sched_ts + 2)
    assert count_dup == 0
    assert len(delivered_messages) == 1

    # Reminder status is now completed
    rem = temp_memory.get_reminder(rid)
    assert rem["status"] == "completed"
    assert rem["last_delivered_at"] is not None


@pytest.mark.asyncio
async def test_scheduler_advances_recurring_reminder_on_delivery(temp_memory):
    """Scheduler advances next occurrence for recurring reminders without completing them."""
    owner_id = 777
    tz = ZoneInfo("Asia/Kolkata")
    sched_dt = datetime(2026, 11, 6, 7, 0, tzinfo=tz)

    rid = temp_memory.create_reminder(
        user_id=owner_id,
        message="Wake up",
        scheduled_at=sched_dt.timestamp(),
        recurrence="daily",
        timezone="Asia/Kolkata",
    )

    deliver_mock = AsyncMock()
    scheduler = ReminderScheduler(
        memory=temp_memory,
        deliver_callback=deliver_mock,
        owner_id=owner_id,
        timezone_name="Asia/Kolkata",
    )

    # Trigger delivery
    count = await scheduler.check_due_reminders(now=sched_dt.timestamp() + 5)
    assert count == 1
    deliver_mock.assert_called_once_with(owner_id, "Reminder: Wake up")

    # Verify reminder remains active with advanced schedule
    rem = temp_memory.get_reminder(rid)
    assert rem["status"] == "active"
    next_dt = datetime.fromtimestamp(rem["scheduled_at"], tz=tz)
    assert next_dt.day == 7
    assert next_dt.hour == 7


# ===========================================================================
# 7. OWNER AUTHORIZATION GUARDS
# ===========================================================================

def test_unauthorized_user_cannot_create_or_view_reminders(temp_memory):
    """Requests from non-owner IDs are rejected with access restriction."""
    owner_id = 1000
    unauthorized_id = 9999
    ctx = {"user_id": unauthorized_id, "owner_id": owner_id, "memory": temp_memory}

    # 1. Create attempt
    res_create = handle_reminder_text("Remind me to study at 8 PM", context=ctx)
    assert res_create is not None
    assert "Access restricted" in res_create["reply"]

    # 2. List attempt
    res_list = handle_reminder_text("Show my reminders", context=ctx)
    assert res_list is not None
    assert "Access restricted" in res_list["reply"]

    # 3. Cancel attempt
    res_cancel = handle_reminder_text("Cancel reminder 1", context=ctx)
    assert res_cancel is not None
    assert "Access restricted" in res_cancel["reply"]


@pytest.mark.asyncio
async def test_scheduler_skips_unauthorized_user_reminder(temp_memory):
    """Scheduler refuses to deliver reminders to any user other than authorized owner."""
    owner_id = 1000
    intruder_id = 6666

    # Insert a reminder belonging to an unauthorized user
    temp_memory.create_reminder(
        user_id=intruder_id,
        message="Malicious trigger",
        scheduled_at=time.time() - 10,
    )

    deliver_mock = AsyncMock()
    scheduler = ReminderScheduler(
        memory=temp_memory,
        deliver_callback=deliver_mock,
        owner_id=owner_id,
    )

    count = await scheduler.check_due_reminders()
    assert count == 0
    deliver_mock.assert_not_called()


# ===========================================================================
# 8. TELEGRAM BOT SLASH COMMAND INTEGRATION
# ===========================================================================

@pytest.mark.asyncio
async def test_telegram_app_reminders_slash_commands(temp_memory):
    """Telegram bot registers /reminders and /cancel_reminder handlers properly."""
    owner_id = 555
    mock_scheduler = MagicMock()
    mock_scheduler.start = MagicMock()
    mock_scheduler.stop = AsyncMock()

    app = create_telegram_app(
        token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        owner_id=owner_id,
        memory=temp_memory,
        scheduler=mock_scheduler,
    )

    assert app.bot_data.get("reminder_scheduler") == mock_scheduler

    # Verify command handlers are registered
    handlers = app.handlers[0]
    command_names = []
    for h in handlers:
        if hasattr(h, "commands"):
            command_names.extend(h.commands)

    assert "reminders" in command_names
    assert "cancel_reminder" in command_names


# ===========================================================================
# 9. STEP 17.1: MANUAL LIFECYCLE & FAILURE PROPAGATION
# ===========================================================================

@pytest.mark.asyncio
async def test_fastapi_manual_lifecycle_invokes_post_init_and_post_shutdown(monkeypatch):
    """FastAPI lifespan explicitly calls post_init and post_shutdown for manual PTB lifecycle."""
    from orion.main import lifespan, app

    mock_telegram_app = MagicMock()
    mock_telegram_app.initialize = AsyncMock()
    mock_telegram_app.post_init = AsyncMock()
    mock_telegram_app.start = AsyncMock()
    mock_telegram_app.updater = MagicMock()
    mock_telegram_app.updater.running = True
    mock_telegram_app.updater.start_polling = AsyncMock()
    mock_telegram_app.updater.stop = AsyncMock()
    mock_telegram_app.stop = AsyncMock()
    mock_telegram_app.post_shutdown = AsyncMock()
    mock_telegram_app.shutdown = AsyncMock()

    mock_settings = MagicMock()
    mock_settings.telegram_bot_token = "mock_token"
    mock_settings.telegram_owner_id = 12345
    mock_settings.app_name = "ORION"
    mock_settings.debug = False

    monkeypatch.setattr("orion.main.get_settings", lambda: mock_settings)
    monkeypatch.setattr("orion.main.create_telegram_app", lambda token, owner_id: mock_telegram_app)

    async with lifespan(app):
        # Startup checks
        mock_telegram_app.initialize.assert_awaited_once()
        mock_telegram_app.post_init.assert_awaited_once_with(mock_telegram_app)
        mock_telegram_app.start.assert_awaited_once()
        mock_telegram_app.updater.start_polling.assert_awaited_once()

    # Shutdown checks
    mock_telegram_app.updater.stop.assert_awaited_once()
    mock_telegram_app.stop.assert_awaited_once()
    mock_telegram_app.post_shutdown.assert_awaited_once_with(mock_telegram_app)
    mock_telegram_app.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_real_telegram_app_scheduler_starts_via_post_init_and_stops_via_post_shutdown(temp_memory):
    """Calling post_init and post_shutdown on real create_telegram_app controls scheduler lifecycle."""
    owner_id = 888
    app = create_telegram_app(
        token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        owner_id=owner_id,
        memory=temp_memory,
    )
    scheduler = app.bot_data["reminder_scheduler"]
    assert scheduler.is_running is False

    # 1. Trigger post_init
    await app.post_init(app)
    assert scheduler.is_running is True

    # 2. Trigger post_shutdown
    await app.post_shutdown(app)
    assert scheduler.is_running is False


@pytest.mark.asyncio
async def test_failed_send_propagates_and_reminder_remains_active(temp_memory):
    """When Telegram send fails, failure propagates to scheduler and reminder is NOT marked delivered."""
    owner_id = 888
    sched_ts = time.time() - 5
    rid = temp_memory.create_reminder(
        user_id=owner_id,
        message="Important meeting",
        scheduled_at=sched_ts,
    )

    app = create_telegram_app(
        token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        owner_id=owner_id,
        memory=temp_memory,
    )
    # Mock bot.send_message to raise network exception
    app.bot._frozen = False
    app.bot.send_message = AsyncMock(side_effect=RuntimeError("Telegram network connection failed"))

    scheduler = app.bot_data["reminder_scheduler"]
    count = await scheduler.check_due_reminders(now=sched_ts + 10)

    # Delivery failed: count must be 0
    assert count == 0

    # Reminder must remain ACTIVE in SQLite (not completed!)
    rem = temp_memory.get_reminder(rid)
    assert rem["status"] == "active"
    assert rem["last_delivered_at"] is None


@pytest.mark.asyncio
async def test_html_format_failure_falls_back_to_plain_text(temp_memory):
    """When formatted send fails, plain text fallback delivers and marks reminder completed."""
    owner_id = 888
    sched_ts = time.time() - 5
    rid = temp_memory.create_reminder(
        user_id=owner_id,
        message="Review <unclosed code",
        scheduled_at=sched_ts,
    )

    app = create_telegram_app(
        token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        owner_id=owner_id,
        memory=temp_memory,
    )

    # First call (HTML) fails, second call (plain text) succeeds
    app.bot._frozen = False
    app.bot.send_message = AsyncMock(
        side_effect=[Exception("Can't parse entities in message"), None]
    )

    scheduler = app.bot_data["reminder_scheduler"]
    count = await scheduler.check_due_reminders(now=sched_ts + 10)

    assert count == 1
    assert app.bot.send_message.call_count == 2

    # Second call must be plain text without parse_mode=HTML
    second_call_kwargs = app.bot.send_message.call_args_list[1][1]
    assert "parse_mode" not in second_call_kwargs or second_call_kwargs.get("parse_mode") != "HTML"

    # Reminder is successfully marked completed
    rem = temp_memory.get_reminder(rid)
    assert rem["status"] == "completed"
    assert rem["last_delivered_at"] is not None

