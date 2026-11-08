"""Personal reminder management, parsing, and lightweight scheduling for ORION (Step 17)."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import re
import time
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from orion.config.settings import get_settings
from orion.memory.database import ConversationMemory

logger = logging.getLogger("orion.core.reminders")


@dataclass
class ReminderParseResult:
    """Structured result of parsing a natural language reminder request."""

    is_reminder_intent: bool = False
    message: str | None = None
    scheduled_at: float | None = None
    recurrence: str | None = None  # None, "daily", "weekly:<day>", "weekdays"
    target_time_display: str | None = None
    needs_clarification: bool = False
    clarification_prompt: str | None = None


# Pattern to identify time expressions within a reminder command
TIME_EXPRESSION_REGEX = re.compile(
    r"(?i)\b("
    r"(?:every\s+(?:day|weekday|weekdays|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|daily)"
    r"(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?|\s+at\s+noon|\s+at\s+midnight)?"
    r"|in\s+\d+\s*(?:minutes?|mins?|hours?|hrs?|seconds?|secs?|days?)"
    r"|(?:tomorrow|today|tonight)(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?|\s+at\s+noon|\s+at\s+midnight)?"
    r"|(?:(?:next|on)\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?|\s+at\s+noon|\s+at\s+midnight)"
    r"|at\s+(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)?|noon|midnight)"
    r")\b"
)


def parse_time_part(
    time_str: str,
    now_dt: datetime,
) -> tuple[datetime | None, str | None, str | None]:
    """Parse time string into (target_datetime, recurrence, display_string).

    `now_dt` must be a timezone-aware datetime.
    """
    t_clean = time_str.lower().strip()

    # 1. Recurrence detection
    recurrence: str | None = None
    if re.search(r"\bevery\s+day\b|\bdaily\b", t_clean):
        recurrence = "daily"
    elif re.search(r"\bevery\s+weekday\b|\bweekdays\b", t_clean):
        recurrence = "weekdays"
    else:
        m_day = re.search(
            r"\bevery\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
            t_clean,
        )
        if m_day:
            recurrence = f"weekly:{m_day.group(1)}"

    # 2. Relative time: "in X minutes/hours/seconds"
    m_rel = re.search(
        r"\bin\s+(\d+)\s*(minutes?|mins?|hours?|hrs?|seconds?|secs?|days?)\b",
        t_clean,
    )
    if m_rel:
        val = int(m_rel.group(1))
        unit = m_rel.group(2)
        if "min" in unit:
            target = now_dt + timedelta(minutes=val)
        elif "hour" in unit or "hr" in unit:
            target = now_dt + timedelta(hours=val)
        elif "sec" in unit:
            target = now_dt + timedelta(seconds=val)
        elif "day" in unit:
            target = now_dt + timedelta(days=val)
        else:
            target = now_dt + timedelta(minutes=val)
        return target, recurrence, f"in {val} {unit}"

    # 3. Time of day (hours, minutes)
    hour: int | None = None
    minute: int = 0
    m_tod = re.search(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", t_clean)
    if not m_tod:
        m_tod = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", t_clean)

    if m_tod:
        h = int(m_tod.group(1))
        m = int(m_tod.group(2)) if m_tod.group(2) else 0
        ampm = m_tod.group(3)
        if ampm:
            ampm = ampm.lower()
            if ampm == "pm" and h < 12:
                h += 12
            elif ampm == "am" and h == 12:
                h = 0
        hour = h
        minute = m
    elif "noon" in t_clean:
        hour = 12
        minute = 0
    elif "midnight" in t_clean:
        hour = 0
        minute = 0
    elif "tonight" in t_clean:
        hour = 20
        minute = 0

    if hour is None:
        return None, recurrence, None

    # 4. Target date calculation
    target_date = now_dt.date()
    if "tomorrow" in t_clean:
        target_date += timedelta(days=1)
    elif recurrence == "daily":
        cand = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if cand <= now_dt:
            target_date += timedelta(days=1)
    elif recurrence and recurrence.startswith("weekly:"):
        day_name = recurrence.split(":", 1)[1]
        wd_map = {
            "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6,
        }
        target_wd = wd_map[day_name]
        days_ahead = (target_wd - now_dt.weekday()) % 7
        cand = (now_dt + timedelta(days=days_ahead)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if cand <= now_dt:
            days_ahead += 7
        target_date = now_dt.date() + timedelta(days=days_ahead)
    elif recurrence == "weekdays":
        cand = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        days_ahead = 1 if cand <= now_dt else 0
        cand_date = now_dt.date() + timedelta(days=days_ahead)
        while cand_date.weekday() >= 5:
            cand_date += timedelta(days=1)
        target_date = cand_date
    else:
        # Check specific day of week (e.g. "on Friday at 4 PM")
        m_day = re.search(
            r"\b(?:next\s+|on\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
            t_clean,
        )
        if m_day:
            day_name = m_day.group(1)
            wd_map = {
                "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                "friday": 4, "saturday": 5, "sunday": 6,
            }
            target_wd = wd_map[day_name]
            days_ahead = (target_wd - now_dt.weekday()) % 7
            cand = (now_dt + timedelta(days=days_ahead)).replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            if cand <= now_dt:
                days_ahead += 7
            target_date = now_dt.date() + timedelta(days=days_ahead)
        else:
            # Standalone time of day (e.g. "at 8 PM")
            cand = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if cand <= now_dt:
                target_date += timedelta(days=1)

    target_dt = now_dt.replace(
        year=target_date.year,
        month=target_date.month,
        day=target_date.day,
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    )

    # Clean display formatting
    time_fmt = target_dt.strftime("%I:%M %p").lstrip("0")
    if recurrence == "daily":
        disp = f"every day at {time_fmt}"
    elif recurrence and recurrence.startswith("weekly:"):
        day_cap = recurrence.split(":", 1)[1].title()
        disp = f"every {day_cap} at {time_fmt}"
    elif recurrence == "weekdays":
        disp = f"every weekday at {time_fmt}"
    elif target_date == now_dt.date():
        disp = f"today at {time_fmt}"
    elif target_date == (now_dt.date() + timedelta(days=1)):
        disp = f"tomorrow at {time_fmt}"
    else:
        disp = target_dt.strftime("%A, %b %d at ") + time_fmt

    return target_dt, recurrence, disp


def parse_reminder_request(
    text: str,
    now_dt: datetime | None = None,
    tz_name: str = "Asia/Kolkata",
) -> ReminderParseResult:
    """Parse user request into a ReminderParseResult."""
    clean = text.strip()
    match = re.match(
        r"^(?:(?:hey\s+|hi\s+)?orion[,\s]+)?(?:please\s+)?(?:can\s+you\s+|could\s+you\s+)?(?:remind\s+me|set\s+(?:a\s+)?reminder)\b\s*(.*)$",
        clean,
        re.IGNORECASE,
    )
    if not match:
        return ReminderParseResult(is_reminder_intent=False)

    remainder = match.group(1).strip()
    if not remainder:
        return ReminderParseResult(
            is_reminder_intent=True,
            needs_clarification=True,
            clarification_prompt="What would you like me to remind you about, and when?",
        )

    # Exclude informational/factual questions like "Remind me who the president is"
    first_word = remainder.split()[0].lower() if remainder.split() else ""
    if first_word in ("who", "what", "where", "when", "why", "how", "which", "if", "whether"):
        # If there is no explicit future time expression, treat as conversational
        if not TIME_EXPRESSION_REGEX.search(remainder):
            return ReminderParseResult(is_reminder_intent=False)

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    current_now = now_dt if now_dt is not None else datetime.now(tz)
    if current_now.tzinfo is None:
        current_now = current_now.replace(tzinfo=tz)

    time_match = TIME_EXPRESSION_REGEX.search(remainder)
    if not time_match:
        # User specified an action but no time (e.g. "Remind me to call mom")
        msg_candidate = re.sub(r"^(?:to|about|for)\s+", "", remainder, flags=re.IGNORECASE).strip(" .!?\"'")
        return ReminderParseResult(
            is_reminder_intent=True,
            message=msg_candidate,
            needs_clarification=True,
            clarification_prompt=(
                f"When would you like me to remind you to {msg_candidate}? "
                "Please specify a time or date (e.g. 'at 8 PM', 'tomorrow at 10 AM', or 'every day at 7 AM')."
            ),
        )

    time_str = time_match.group(1).strip()

    # Extract the message part from text before or after the time expression
    before_part = remainder[:time_match.start()].strip()
    after_part = remainder[time_match.end():].strip()
    combined_msg = (before_part + " " + after_part).strip()

    # Clean leading/trailing connector prepositions
    clean_msg = re.sub(r"^(?:to|about|for)\s+", "", combined_msg, flags=re.IGNORECASE).strip()
    clean_msg = re.sub(r"\s+(?:to|about|for)$", "", clean_msg, flags=re.IGNORECASE).strip(" .!?\"'")

    if not clean_msg:
        # User specified a time but no message (e.g. "Remind me at 8 PM")
        return ReminderParseResult(
            is_reminder_intent=True,
            needs_clarification=True,
            clarification_prompt=f"What would you like me to remind you about {time_str}?",
        )

    target_dt, recurrence, disp = parse_time_part(time_str, current_now)
    if not target_dt or not disp:
        return ReminderParseResult(
            is_reminder_intent=True,
            message=clean_msg,
            needs_clarification=True,
            clarification_prompt=(
                f"I couldn't quite understand the scheduled time '{time_str}'. "
                "Could you please specify a clear time (e.g. 'at 8 PM', 'tomorrow at 10 AM', or 'every day at 7 AM')?"
            ),
        )

    return ReminderParseResult(
        is_reminder_intent=True,
        message=clean_msg,
        scheduled_at=target_dt.timestamp(),
        recurrence=recurrence,
        target_time_display=disp,
        needs_clarification=False,
    )


def calculate_next_occurrence(
    reminder: dict[str, Any],
    now: float | None = None,
    tz_name: str = "Asia/Kolkata",
) -> float | None:
    """Calculate the next occurrence timestamp for a recurring reminder.

    Returns None for non-recurring reminders.
    """
    recurrence = reminder.get("recurrence")
    if not recurrence:
        return None

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    now_val = now if now is not None else time.time()
    now_dt = datetime.fromtimestamp(now_val, tz=tz)

    orig_dt = datetime.fromtimestamp(reminder["scheduled_at"], tz=tz)
    target_hour = orig_dt.hour
    target_minute = orig_dt.minute
    target_second = orig_dt.second

    if recurrence == "daily":
        next_dt = now_dt.replace(
            hour=target_hour, minute=target_minute, second=target_second, microsecond=0
        )
        if next_dt <= now_dt:
            next_dt += timedelta(days=1)
        while next_dt <= now_dt:
            next_dt += timedelta(days=1)
        return next_dt.timestamp()

    elif recurrence.startswith("weekly:"):
        target_weekday_name = recurrence.split(":", 1)[1].lower()
        weekday_map = {
            "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6,
        }
        target_wd = weekday_map.get(target_weekday_name)
        if target_wd is None:
            next_dt = orig_dt + timedelta(days=7)
            while next_dt <= now_dt:
                next_dt += timedelta(days=7)
            return next_dt.timestamp()

        days_ahead = (target_wd - now_dt.weekday()) % 7
        next_dt = (now_dt + timedelta(days=days_ahead)).replace(
            hour=target_hour, minute=target_minute, second=target_second, microsecond=0
        )
        if next_dt <= now_dt:
            next_dt += timedelta(days=7)
        while next_dt <= now_dt:
            next_dt += timedelta(days=7)
        return next_dt.timestamp()

    elif recurrence == "weekdays":
        next_dt = now_dt.replace(
            hour=target_hour, minute=target_minute, second=target_second, microsecond=0
        )
        if next_dt <= now_dt:
            next_dt += timedelta(days=1)
        while next_dt <= now_dt or next_dt.weekday() >= 5:
            next_dt += timedelta(days=1)
        return next_dt.timestamp()

    return None


def format_reminder_list(
    reminders: list[dict[str, Any]],
    tz_name: str = "Asia/Kolkata",
) -> str:
    """Format active reminders into a clean, friendly Telegram message."""
    if not reminders:
        return "You have no active reminders."

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    now_dt = datetime.now(tz)
    lines = ["⏰ **Your Active Reminders:**\n"]

    for i, r in enumerate(reminders, 1):
        rid = r["id"]
        msg = r["message"]
        rec = r.get("recurrence")
        sched_dt = datetime.fromtimestamp(r["scheduled_at"], tz=tz)

        time_fmt = sched_dt.strftime("%I:%M %p").lstrip("0")
        if rec == "daily":
            sched_label = f"Every day at {time_fmt}"
        elif rec and rec.startswith("weekly:"):
            day_cap = rec.split(":", 1)[1].title()
            sched_label = f"Every {day_cap} at {time_fmt}"
        elif rec == "weekdays":
            sched_label = f"Every weekday at {time_fmt}"
        elif sched_dt.date() == now_dt.date():
            sched_label = f"Today at {time_fmt}"
        elif sched_dt.date() == (now_dt.date() + timedelta(days=1)):
            sched_label = f"Tomorrow at {time_fmt}"
        else:
            sched_label = sched_dt.strftime("%A, %b %d at ") + time_fmt

        lines.append(f"{i}. **{msg}**")
        lines.append(f"   • Scheduled: {sched_label} ({tz_name})")
        lines.append(f"   • ID: {rid}")
        lines.append("")

    lines.append("*To cancel a reminder, send `/cancel_reminder <id>` or 'Cancel reminder <id>'.*")
    return "\n".join(lines).strip()


def handle_reminder_text(
    text: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Deterministically intercept and handle reminder commands.

    Returns dict with handled=True and reply string, or None if text is not a reminder command.
    """
    clean = text.strip()
    if not clean:
        return None

    settings = get_settings()
    tz_name = settings.timezone

    ctx = context or {}
    user_id = ctx.get("user_id") or ctx.get("owner_id")
    owner_id = ctx.get("owner_id")
    memory: ConversationMemory | None = ctx.get("memory")

    # 1. Show / List reminders
    list_pattern = r"^(?:/reminders|show\s+(?:all\s+)?(?:my\s+)?reminders|list\s+(?:all\s+)?(?:my\s+)?reminders|view\s+(?:all\s+)?(?:my\s+)?reminders|(?:my\s+)?reminders)$"
    if re.match(list_pattern, clean, re.IGNORECASE):
        if owner_id and user_id != owner_id:
            return {"handled": True, "reply": "Access restricted: unauthorized user or chat."}
        if not memory or not user_id:
            return {"handled": True, "reply": "You have no active reminders."}

        active = memory.get_active_reminders(user_id)
        reply = format_reminder_list(active, tz_name=tz_name)
        return {"handled": True, "reply": reply}

    # 2. Cancel / Delete reminder
    cancel_match = re.match(
        r"^(?:/cancel_reminder|cancel\s+reminder|delete\s+reminder|remove\s+reminder)(?:\s*#?\s*(\d+))?$",
        clean,
        re.IGNORECASE,
    )
    if cancel_match:
        if owner_id and user_id != owner_id:
            return {"handled": True, "reply": "Access restricted: unauthorized user or chat."}

        rid_str = cancel_match.group(1)
        if not rid_str:
            return {
                "handled": True,
                "reply": "Which reminder would you like to cancel? Send `/reminders` to view your active reminders with their IDs.",
            }

        rid = int(rid_str)
        if not memory or not user_id:
            return {"handled": True, "reply": f"Reminder {rid} was not found or is already cancelled."}

        rem = memory.get_reminder(rid, user_id=user_id)
        success = memory.cancel_reminder(rid, user_id=user_id)
        if success:
            label = f" ('{rem['message']}')" if rem and rem.get("message") else ""
            return {"handled": True, "reply": f"✅ Reminder {rid}{label} has been cancelled."}
        else:
            return {"handled": True, "reply": f"Reminder {rid} was not found or is already cancelled."}

    # 3. Create reminder
    parse_res = parse_reminder_request(clean, tz_name=tz_name)
    if not parse_res.is_reminder_intent:
        return None

    if owner_id and user_id != owner_id:
        return {"handled": True, "reply": "Access restricted: unauthorized user or chat."}

    if parse_res.needs_clarification:
        return {"handled": True, "reply": parse_res.clarification_prompt}

    if not memory or not user_id:
        return {"handled": True, "reply": "Unable to save reminder: memory service unavailable."}

    rid = memory.create_reminder(
        user_id=user_id,
        message=parse_res.message,  # type: ignore
        scheduled_at=parse_res.scheduled_at,  # type: ignore
        recurrence=parse_res.recurrence,
        timezone=tz_name,
    )

    confirm_reply = f"✅ I'll remind you to {parse_res.message} {parse_res.target_time_display}."
    return {
        "handled": True,
        "reply": confirm_reply,
        "reminder_id": rid,
        "scheduled_at": parse_res.scheduled_at,
        "recurrence": parse_res.recurrence,
    }


class ReminderScheduler:
    """Lightweight local scheduler for delivering persistent Telegram reminders."""

    def __init__(
        self,
        memory: ConversationMemory,
        deliver_callback: Callable[[int, str], Awaitable[None]],
        owner_id: int,
        check_interval: float = 10.0,
        timezone_name: str = "Asia/Kolkata",
    ) -> None:
        self.memory = memory
        self.deliver_callback = deliver_callback
        self.owner_id = owner_id
        self.check_interval = check_interval
        self.timezone_name = timezone_name
        self._task: asyncio.Task | None = None
        self._running: bool = False
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        """Whether the reminder scheduler loop is currently active."""
        return self._running and self._task is not None and not self._task.done()

    def start(self) -> asyncio.Task:
        """Start the background reminder checking loop."""
        if self._running and self._task and not self._task.done():
            return self._task
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "ReminderScheduler started (interval: %.1fs, tz: %s)",
            self.check_interval,
            self.timezone_name,
        )
        return self._task

    async def stop(self) -> None:
        """Stop the background reminder checking loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("ReminderScheduler stopped")

    async def check_due_reminders(self, now: float | None = None) -> int:
        """Check for due reminders, deliver them, and advance recurrence atomically.

        Returns the number of reminders successfully delivered.
        """
        current_now = now if now is not None else time.time()
        delivered_count = 0

        async with self._lock:
            due_reminders = self.memory.get_due_reminders(now=current_now)
            for r in due_reminders:
                rid = r["id"]
                user_id = r["user_id"]
                msg = r["message"]
                tz_name = r.get("timezone") or self.timezone_name

                # Security check: owner-only access
                if self.owner_id and user_id != self.owner_id:
                    logger.warning(
                        "Skipping reminder %s: user %s is not authorized owner %s",
                        rid,
                        user_id,
                        self.owner_id,
                    )
                    continue

                # Duplicate protection: skip if delivered within the last 30 seconds
                last_delivered = r.get("last_delivered_at")
                if last_delivered and (current_now - float(last_delivered)) < 30.0:
                    continue

                # Deliver the reminder message
                delivery_text = f"Reminder: {msg}"
                try:
                    await self.deliver_callback(user_id, delivery_text)
                    delivered_count += 1
                except Exception as err:
                    logger.error("Failed to deliver reminder %s to user %s: %s", rid, user_id, err)
                    continue

                # Update SQLite: complete one-time or advance recurring schedule
                next_time = calculate_next_occurrence(r, now=current_now, tz_name=tz_name)
                self.memory.mark_reminder_delivered(
                    rid,
                    delivered_at=current_now,
                    next_scheduled_at=next_time,
                )
                logger.info(
                    "Delivered reminder %s ('%s') to %s; next_scheduled: %s",
                    rid,
                    msg,
                    user_id,
                    next_time,
                )

        return delivered_count

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self.check_due_reminders()
            except asyncio.CancelledError:
                break
            except Exception as err:
                logger.error("Unexpected error in reminder check loop: %s", err)

            try:
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break


class ReminderManager:
    """Manager for querying and coordinating personal reminders."""

    def __init__(
        self,
        db_path: str | None = None,
        memory: ConversationMemory | None = None,
    ) -> None:
        self.db_path = str(db_path) if db_path is not None else None
        self.memory = memory or (ConversationMemory(db_path=self.db_path) if self.db_path else ConversationMemory())

    def get_upcoming_reminders(
        self,
        hours: float = 24.0,
        user_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieve active reminders scheduled within the next N hours."""
        now_ts = time.time()
        max_ts = now_ts + (hours * 3600)
        with self.memory._lock:
            with self.memory._get_connection() as conn:
                if user_id is not None:
                    cur = conn.execute(
                        """
                        SELECT id, user_id, message, scheduled_at, recurrence, timezone, status
                        FROM reminders
                        WHERE user_id = ? AND status = 'active' AND scheduled_at <= ?
                        ORDER BY scheduled_at ASC
                        """,
                        (user_id, max_ts),
                    )
                else:
                    cur = conn.execute(
                        """
                        SELECT id, user_id, message, scheduled_at, recurrence, timezone, status
                        FROM reminders
                        WHERE status = 'active' AND scheduled_at <= ?
                        ORDER BY scheduled_at ASC
                        """,
                        (max_ts,),
                    )
                rows = cur.fetchall()
                return [
                    {
                        "id": r[0],
                        "user_id": r[1],
                        "message": r[2],
                        "scheduled_at": float(r[3]),
                        "recurrence": r[4],
                        "timezone": r[5],
                        "status": r[6],
                    }
                    for r in rows
                ]
