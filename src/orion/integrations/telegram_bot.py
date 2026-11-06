"""Telegram bot integration for ORION using long polling."""

import asyncio
import logging
import re
from typing import Any
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from orion.config.settings import get_settings
from orion.core.planner import generate_task_plan
from orion.integrations.formatter import (
    clean_markdown_to_plain,
    markdown_to_telegram_html,
    split_html_message,
)
from orion.integrations.ollama import OLLAMA_UNAVAILABLE_MESSAGE, OllamaClient
from orion.memory.database import ConversationMemory
from orion.tools.app_launcher import get_approved_apps, is_app_available
from orion.tools.registry import ToolRegistry, default_tool_registry
from orion.tools.router import format_tool_result, route_and_execute_tool
from orion.voice.transcriber import VoiceTranscriber

logger = logging.getLogger("orion.integrations.telegram_bot")

UNAUTHORIZED_MESSAGE = "Access restricted: unauthorized user or chat."
WELCOME_MESSAGE = "Hello! I am ORION, your personal AI assistant. How can I help you today?"
HELP_MESSAGE = (
    "**ORION Commands:**\n"
    "• `/status` — View current running and pending task status\n"
    "• `/history` — View recent task activity history\n"
    "• `/windows` — View open approved application windows\n"
    "• `/active` — View currently active approved application\n"
    "• `/apps` — View available approved Windows applications\n"
    "• `/cancel` — Cancel pending execution proposals\n"
    "• `/plan <goal>` — Generate structured action plan\n"
    "• `/tasks` — View recent action plans\n"
    "• `/task <id>` — View details of a specific plan\n"
    "• `/reset` — Clear conversation history\n"
    "\n*Window management (focus/minimize/maximize/restore) is done via natural language + confirmation.*"
)

# Display names for action_type values used in history/status
ACTION_TYPE_DISPLAY: dict[str, str] = {
    "focus_application_window": "Focus window",
    "minimize_application_window": "Minimize window",
    "maximize_application_window": "Maximize window",
    "restore_application_window": "Restore window",
    "launch_application": "Launch application",
}

_WINDOW_MANAGEMENT_ACTION_TYPES = frozenset({
    "focus_application_window",
    "minimize_application_window",
    "maximize_application_window",
    "restore_application_window",
})
RESET_CONFIRMATION_PROMPT = (
    "Are you sure you want to clear your conversation history? "
    "To proceed, send:\n`/reset confirm`"
)
RESET_SUCCESS_MESSAGE = "Conversation memory has been cleared."


def split_message(text: str, max_chunk: int = 4000) -> list[str]:
    """Split response into chunks safe for Telegram's character limit."""
    return split_html_message(text, max_chunk=max_chunk)


async def send_formatted_message(message: Any, text: str) -> None:
    """Send text formatted as HTML with fallback to clean plain text on parse error."""
    html_text = markdown_to_telegram_html(text)
    chunks = split_html_message(html_text)

    for chunk in chunks:
        try:
            await message.reply_text(chunk, parse_mode=ParseMode.HTML)
        except Exception:
            plain_chunk = clean_markdown_to_plain(chunk)
            await message.reply_text(plain_chunk)


def format_plan_message(plan_id: int, goal: str, steps: list[dict[str, Any]]) -> str:
    """Format an action plan cleanly with steps and clear unexecuted status."""
    lines = [
        f"**Action Plan #{plan_id}**",
        f"**Goal:** {goal}",
        "**Status:** Planned — not executed\n",
        "**Proposed Steps:**",
    ]
    for step in steps:
        step_id = step.get("id", "-")
        desc = step.get("description", "").strip()
        lines.append(f"{step_id}. {desc} [Planned]")
    lines.append("\n*Note: Planned action proposals — not executed.*")
    return "\n".join(lines)


def is_authorized_owner(update: Update, owner_id: int) -> bool:
    """Check if the update originates from a private chat with the owner."""
    if not update.effective_chat or update.effective_chat.type != "private":
        return False
    if not update.effective_user or update.effective_user.id != owner_id:
        return False
    return True


def interpret_voice_command(
    transcript: str,
    recent_context: dict[str, Any] | None = None,
) -> tuple[str, bool, str | None]:
    """Interpret speech transcript using current task and window context.

    Returns:
        (interpreted_text, is_ambiguous, clarification_prompt)
        - If is_ambiguous is True, clarification_prompt should be sent to the user.
        - Otherwise, interpreted_text should be processed through the command pipeline.
    """
    raw = (transcript or "").strip()
    if not raw:
        return "", False, None

    # Strip speech transcript punctuation and whitespace
    clean = raw.strip(" .!?\"'")

    # 1. Check if an active pending window selection exists
    from orion.tools.desktop_inspector import (
        is_pending_window_selection_active,
        parse_window_choice,
    )

    if is_pending_window_selection_active():
        idx = parse_window_choice(clean)
        if idx is not None:
            # Normalized window choice representation (e.g. "1", "2")
            return str(idx), False, None

    # 2. Phonetic corrections & transcription slip normalization for approved apps & window verbs
    # Strictly applied ONLY in app-command contexts (not ordinary conversation, file names, paths, or document contents)
    is_app_cmd = bool(re.match(
        r"^(?:(?:hey\s+|hi\s+)?orion[,\s]+)?(?:please\s+)?(?:can\s+you\s+|could\s+you\s+)?(open|launch|start|run|maximize|maximise|minimize|minimise|restore|focus|bring|switch|is|how\s+many)\b",
        clean,
        re.IGNORECASE,
    ))

    normalized = clean
    if is_app_cmd:
        phonetic_subs = [
            (r"\b(?:call\s*c|cal\s*c|cal\s*see)\b", "calc"),
            (r"\bv\s*s\s*code\b", "vs code"),
            (r"\b(?:we|wee)\s*s\s*code\b", "vs code"),
            (r"\bnode\s+pad\b", "notepad"),
            (r"\bnote\s+pad\b", "notepad"),
            (r"\bnot\s+pad\b", "notepad"),
            (r"\bpower\s+point\b", "powerpoint"),
            (r"\btask\s+mgr\b", "task manager"),
            (r"\bwindows\s+explorer\b", "file explorer"),
            (r"\bmaximise\b", "maximize"),
            (r"\bminimise\b", "minimize"),
        ]
        for pattern, repl in phonetic_subs:
            normalized = re.sub(pattern, repl, normalized, flags=re.IGNORECASE)

    # 3. Contextual pronoun resolution: e.g. "maximize it", "bring it to the front"
    m_pronoun = re.match(
        r"^(?:(?:hey\s+|hi\s+)?orion[,\s]+)?(?:please\s+)?(maximize|maximise|minimize|minimise|restore|focus|bring\s+(?:it\s+|that\s+)?to\s+(?:the\s+)?(?:front|foreground)|switch\s+to)\s+(?:it|this|that|the\s+app|the\s+window|that\s+app|that\s+window)[\s.!?]*$",
        normalized,
        flags=re.IGNORECASE,
    )
    if not m_pronoun:
        m_pronoun = re.match(
            r"^(?:(?:hey\s+|hi\s+)?orion[,\s]+)?(?:please\s+)?bring\s+(?:it|that|this)\s+(?:to\s+(?:the\s+)?(?:front|foreground)|up)[\s.!?]*$",
            normalized,
            flags=re.IGNORECASE,
        )
        verb = "bring to the front" if m_pronoun else None
    else:
        raw_verb = m_pronoun.group(1).strip()
        verb = "bring to the front" if "front" in raw_verb.lower() or "foreground" in raw_verb.lower() or raw_verb.lower() == "focus" else raw_verb.lower()

    if verb:
        recent_app = None
        reason = "no_context"
        if recent_context:
            if "recent_app_id" in recent_context:
                recent_app = recent_context["recent_app_id"]
                reason = None
            elif "memory" in recent_context and ("owner_id" in recent_context or "user_id" in recent_context):
                mem = recent_context["memory"]
                oid = recent_context.get("owner_id") if recent_context.get("owner_id") is not None else recent_context.get("user_id")
                if hasattr(mem, "get_last_targeted_app"):
                    recent_app, reason = mem.get_last_targeted_app(oid)
                elif hasattr(mem, "get_recent_action_history"):
                    history = mem.get_recent_action_history(oid, limit=3)
                    for h in history:
                        payload = h.get("payload") or {}
                        if isinstance(payload, dict) and payload.get("app_id"):
                            recent_app = payload["app_id"]
                            break
                        elif isinstance(payload, dict) and payload.get("target_app_id"):
                            recent_app = payload["target_app_id"]
                            break

        if recent_app:
            normalized = f"{verb} {recent_app}"
        else:
            return (
                normalized,
                True,
                f"Which application would you like to {verb}?",
            )

    # 4. Sensitive / Destructive operation guard:
    # Never silently change uncertain commands into destructive or sensitive operations.
    destructive_keywords = {"delete", "remove", "erase", "overwrite", "wipe", "format", "destroy", "truncate"}
    words_in_transcript = set(re.findall(r"\b\w+\b", normalized.lower()))
    has_destructive = bool(destructive_keywords & words_in_transcript)
    if has_destructive:
        tokens = normalized.split()
        if len(tokens) <= 3 or any(w in words_in_transcript for w in ("that", "it", "this", "something", "everything", "all")):
            return (
                normalized,
                True,
                "Could you please clarify which file or action you want to perform?",
            )

    # 5. Ambiguous application name guard:
    # If the voice command specifies an ambiguous application name (e.g. "open microsoft"),
    # ask for clarification rather than guessing or executing an unintended app.
    if is_app_cmd:
        m_app = re.match(
            r"^(?:(?:hey\s+|hi\s+)?orion[,\s]+)?(?:please\s+)?(?:can\s+you\s+|could\s+you\s+)?(open|launch|start|run|maximize|maximise|minimize|minimise|restore|focus|bring\s+(?:to\s+(?:the\s+)?front|up))\s+(.+)$",
            normalized,
            re.IGNORECASE,
        )
        if m_app:
            action_verb = m_app.group(1).lower()
            target_cand = m_app.group(2).strip().strip(" .!?\"'")
            target_cand = re.sub(
                r"(?i)\s+(?:on\s+my\s+laptop|on\s+my\s+pc|on\s+my\s+computer|on\s+windows|app|application|program)[\.!\?]?$",
                "",
                target_cand,
            ).strip()

            from orion.tools.app_launcher import find_matching_apps, get_approved_apps

            matches = find_matching_apps(target_cand)
            if len(matches) > 1:
                approved = get_approved_apps()
                names = [approved[aid].display_name for aid in matches if aid in approved]
                act_label = "bring to the front" if "front" in action_verb or "up" in action_verb else action_verb
                return (
                    normalized,
                    True,
                    f"Multiple approved applications matched '{target_cand}': {', '.join(names)}. Which one would you like to {act_label}?",
                )

    return normalized, False, None


def create_telegram_app(
    token: str,
    owner_id: int,
    ollama_client: OllamaClient | None = None,
    memory: ConversationMemory | None = None,
    tool_registry: ToolRegistry | None = None,
    transcriber: VoiceTranscriber | None = None,
) -> Application:
    """Build and configure the Telegram application with handlers."""
    client = ollama_client or OllamaClient()
    conv_memory = memory or ConversationMemory()
    reg = tool_registry or default_tool_registry
    settings = get_settings()
    user_lock = asyncio.Lock()

    # Initialize offline voice transcriber if enabled
    voice_transcriber = transcriber
    if voice_transcriber is None and settings.voice_enabled:
        voice_transcriber = VoiceTranscriber(
            model_size=settings.whisper_model,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
            download_root=settings.whisper_download_root,
        )

    async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized_owner(update, owner_id):
            if update.effective_message:
                await update.effective_message.reply_text(UNAUTHORIZED_MESSAGE)
            return

        if update.effective_message:
            await send_formatted_message(update.effective_message, WELCOME_MESSAGE)

    async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized_owner(update, owner_id):
            if update.effective_message:
                await update.effective_message.reply_text(UNAUTHORIZED_MESSAGE)
            return

        if update.effective_message:
            await send_formatted_message(update.effective_message, HELP_MESSAGE)

    async def reset_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized_owner(update, owner_id):
            if update.effective_message:
                await update.effective_message.reply_text(UNAUTHORIZED_MESSAGE)
            return

        if not update.effective_message:
            return

        args = context.args or []
        if args and args[0].strip().lower() == "confirm":
            async with user_lock:
                conv_memory.clear_history(owner_id)
            await send_formatted_message(update.effective_message, RESET_SUCCESS_MESSAGE)
        else:
            await send_formatted_message(update.effective_message, RESET_CONFIRMATION_PROMPT)

    async def plan_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized_owner(update, owner_id):
            if update.effective_message:
                await update.effective_message.reply_text(UNAUTHORIZED_MESSAGE)
            return

        if not update.effective_message:
            return

        raw_text = update.effective_message.text or ""
        parts = raw_text.split(maxsplit=1)
        goal = parts[1].strip() if len(parts) > 1 else ""

        if not goal:
            await send_formatted_message(
                update.effective_message,
                "Please specify a goal to plan. Example:\n`/plan Organize project documentation`",
            )
            return

        max_len = getattr(settings, "max_goal_length", 500)
        if len(goal) > max_len:
            await send_formatted_message(
                update.effective_message,
                f"Goal is too long. Please keep your goal under {max_len} characters.",
            )
            return

        if update.effective_chat:
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id,
                action=ChatAction.TYPING,
            )

        async with user_lock:
            plan = await generate_task_plan(goal, client=client)
            if not plan or not plan.steps:
                await send_formatted_message(
                    update.effective_message,
                    "Unable to generate a structured action plan for this goal. Please rephrase or provide more detail.",
                )
                return

            plan_dict_steps = [s.model_dump() for s in plan.steps]
            plan_id = conv_memory.save_task_plan(
                user_id=owner_id,
                goal=plan.goal,
                steps=plan_dict_steps,
                status=plan.status,
            )

        formatted_plan = format_plan_message(plan_id, plan.goal, plan_dict_steps)
        await send_formatted_message(update.effective_message, formatted_plan)

    async def tasks_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized_owner(update, owner_id):
            if update.effective_message:
                await update.effective_message.reply_text(UNAUTHORIZED_MESSAGE)
            return

        if not update.effective_message:
            return

        async with user_lock:
            plans = conv_memory.get_recent_task_plans(owner_id, limit=5)

        if not plans:
            await send_formatted_message(
                update.effective_message,
                "No action plans found. Create one using:\n`/plan <goal>`",
            )
            return

        lines = ["**Recent Action Plans:**"]
        for p in plans:
            goal_preview = p["goal"][:60] + ("..." if len(p["goal"]) > 60 else "")
            lines.append(f"• **#{p['id']}**: {goal_preview} [Planned — not executed]")

        lines.append("\n*View details with `/task <id>`*")
        await send_formatted_message(update.effective_message, "\n".join(lines))

    async def task_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized_owner(update, owner_id):
            if update.effective_message:
                await update.effective_message.reply_text(UNAUTHORIZED_MESSAGE)
            return

        if not update.effective_message:
            return

        args = context.args or []
        if not args or not args[0].isdigit():
            await send_formatted_message(
                update.effective_message,
                "Please provide a valid numeric plan ID. Example:\n`/task 1`",
            )
            return

        plan_id = int(args[0])
        async with user_lock:
            plan = conv_memory.get_task_plan(owner_id, plan_id)

        if not plan:
            await send_formatted_message(update.effective_message, "Plan not found.")
            return

        formatted = format_plan_message(plan["id"], plan["goal"], plan["steps"])
        await send_formatted_message(update.effective_message, formatted)

    async def workflow_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle explicit /workflow or /run requests for multi-step filesystem workflows."""
        if not is_authorized_owner(update, owner_id):
            if update.effective_message:
                await update.effective_message.reply_text(UNAUTHORIZED_MESSAGE)
            return

        if not update.effective_message:
            return

        raw_text = update.effective_message.text or ""
        parts = raw_text.split(maxsplit=1)
        goal = parts[1].strip() if len(parts) > 1 else ""

        if not goal:
            await send_formatted_message(
                update.effective_message,
                "Please specify a workflow goal. Example:\n`/workflow Create folder College Notes in Downloads and move notes.pdf into it`",
            )
            return

        if update.effective_chat:
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id,
                action=ChatAction.TYPING,
            )

        async with user_lock:
            from orion.core.execution_engine import (
                format_workflow_proposal,
                generate_execution_plan,
                simulate_and_validate_workflow,
            )

            plan, err = await generate_execution_plan(goal, client=client)
            if not plan:
                await send_formatted_message(
                    update.effective_message,
                    err or "Unable to generate an executable plan for this goal.",
                )
                return

            valid, err_msg, validated_steps = simulate_and_validate_workflow(plan, registry=reg)
            if not valid:
                await send_formatted_message(
                    update.effective_message,
                    f"Cannot execute workflow: {err_msg}",
                )
                return

            workflow_id = conv_memory.create_execution_workflow(
                user_id=owner_id,
                goal=goal,
                steps=validated_steps,
                ttl_seconds=300,
            )
            proposal_text = format_workflow_proposal(goal, validated_steps)
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Approve All", callback_data=f"wf_approve:{workflow_id}"),
                        InlineKeyboardButton("Cancel", callback_data=f"wf_cancel:{workflow_id}"),
                    ]
                ]
            )
            html_msg = markdown_to_telegram_html(proposal_text)
            try:
                await update.effective_message.reply_text(
                    html_msg,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            except Exception:
                await update.effective_message.reply_text(
                    clean_markdown_to_plain(proposal_text),
                    reply_markup=keyboard,
                )
            conv_memory.add_message(owner_id, "user", raw_text)
            conv_memory.add_message(owner_id, "assistant", proposal_text)

    async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Display current running task, pending execution approvals, and latest execution outcome."""
        if not is_authorized_owner(update, owner_id):
            if update.effective_message:
                await update.effective_message.reply_text(UNAUTHORIZED_MESSAGE)
            return

        if not update.effective_message:
            return

        async with user_lock:
            running = conv_memory.get_running_workflow(owner_id)
            pending_wf = conv_memory.get_pending_workflows(owner_id)
            pending_single = conv_memory.get_pending_single_actions(owner_id)
            pending_total = len(pending_wf) + len(pending_single)
            outcome = conv_memory.get_latest_execution_outcome(owner_id)

        lines = ["**ORION — Task Status**\n"]

        if running:
            goal_short = running["goal"][:60] + ("..." if len(running["goal"]) > 60 else "")
            lines.append(f"• **Running:** `{goal_short}`")
        else:
            lines.append("No tasks currently running.")

        lines.append(f"\n**Pending approval:** {pending_total}")

        if outcome:
            summary = outcome.get("summary", "Task")
            status_raw = outcome.get("status", "unknown").lower()
            if status_raw in ("completed", "success"):
                status_str = "Completed successfully."
            elif status_raw == "failed":
                status_str = "Failed."
            elif status_raw == "cancelled":
                status_str = "Cancelled."
            elif status_raw == "interrupted":
                status_str = "Interrupted."
            elif status_raw == "expired":
                status_str = "Expired without approval."
            elif status_raw == "launch requested":
                status_str = "Launch requested."
            elif status_raw == "failed to launch":
                status_str = "Failed to launch."
            else:
                status_str = status_raw.capitalize()
            lines.append(f"**Last task:** {summary} — {status_str}")
        else:
            lines.append("**Last task:** None recorded yet.")

        await send_formatted_message(update.effective_message, "\n".join(lines))

    async def history_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Display the latest 10 execution workflows/actions without sensitive internals."""
        if not is_authorized_owner(update, owner_id):
            if update.effective_message:
                await update.effective_message.reply_text(UNAUTHORIZED_MESSAGE)
            return

        if not update.effective_message:
            return

        async with user_lock:
            history = conv_memory.get_unified_history(owner_id, limit=10)

        if not history:
            await send_formatted_message(update.effective_message, "No recent activity recorded.")
            return

        lines = ["**Recent Activity:**\n"]
        for i, item in enumerate(history, start=1):
            summary = item.get("summary", "Action")
            status_raw = item.get("status", "unknown").lower()
            if status_raw in ("completed", "success"):
                status_str = "Completed"
            elif status_raw == "failed":
                status_str = "Failed"
            elif status_raw == "cancelled":
                status_str = "Cancelled"
            elif status_raw == "expired":
                status_str = "Expired"
            elif status_raw == "interrupted":
                status_str = "Interrupted"
            elif status_raw == "running":
                status_str = "Running"
            elif status_raw == "launch requested":
                status_str = "Launch requested"
            elif status_raw == "failed to launch":
                status_str = "Failed to launch"
            else:
                status_str = status_raw.capitalize()

            ts = item.get("timestamp", 0)
            if ts:
                import datetime

                dt = datetime.datetime.fromtimestamp(ts)
                time_str = dt.strftime("%Y-%m-%d %H:%M")
            else:
                time_str = "N/A"

            step_info = ""
            total = item.get("total_steps", 1)
            if total > 1:
                comp = item.get("completed_steps", 0)
                step_info = f" ({comp}/{total} steps)"

            lines.append(f"{i}. {summary}{step_info} — **{status_str}** *({time_str})*")

        await send_formatted_message(update.effective_message, "\n".join(lines))

    async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /cancel to safely abort pending execution proposals."""
        if not is_authorized_owner(update, owner_id):
            if update.effective_message:
                await update.effective_message.reply_text(UNAUTHORIZED_MESSAGE)
            return

        if not update.effective_message:
            return

        async with user_lock:
            from orion.tools.desktop_inspector import (
                is_pending_window_selection_active,
                clear_pending_window_selection,
            )
            has_pending_win = is_pending_window_selection_active()
            if has_pending_win:
                clear_pending_window_selection()

            running = conv_memory.get_running_workflow(owner_id)
            pending_wf = conv_memory.get_pending_workflows(owner_id)
            pending_single = conv_memory.get_pending_single_actions(owner_id)

            total_pending = len(pending_wf) + len(pending_single)

            if total_pending == 0:
                if running:
                    await send_formatted_message(
                        update.effective_message,
                        "A task is currently running. Running filesystem operations cannot be safely cancelled through this command.",
                    )
                elif has_pending_win:
                    await send_formatted_message(
                        update.effective_message,
                        "Pending window selection cancelled.",
                    )
                else:
                    await send_formatted_message(
                        update.effective_message,
                        "No pending execution proposals to cancel.",
                    )
                return

            if total_pending == 1:
                if pending_wf:
                    item = pending_wf[0]
                    conv_memory.cancel_execution_workflow(item["id"])
                    name = item["goal"]
                    cancel_msg = f"Pending task `{name}` has been cancelled. No filesystem changes were made."
                else:
                    item = pending_single[0]
                    conv_memory.cancel_pending_action(item["id"], owner_id)
                    act_type = item["action_type"]
                    if act_type == "launch_application":
                        app_id = item["payload"].get("app_id", "")
                        app_obj = get_approved_apps().get(app_id)
                        name = app_obj.display_name if app_obj else app_id
                        conv_memory.log_action_history(
                            user_id=owner_id,
                            action_type=act_type,
                            payload=item["payload"],
                            status="cancelled",
                        )
                        cancel_msg = f"Pending launch of `{name}` has been cancelled. No application was launched."
                    elif act_type in _WINDOW_MANAGEMENT_ACTION_TYPES:
                        conv_memory.log_action_history(
                            user_id=owner_id,
                            action_type=act_type,
                            payload=item["payload"],
                            status="cancelled",
                        )
                        cancel_msg = "Pending window operation cancelled. No changes were made."
                    elif act_type == "composite_app_action":
                        conv_memory.log_action_history(
                            user_id=owner_id,
                            action_type=act_type,
                            payload=item["payload"],
                            status="cancelled",
                        )
                        cancel_msg = "Pending application actions were cancelled. No applications were launched."
                    else:
                        name = act_type.replace("_", " ").title()
                        cancel_msg = f"Pending task `{name}` has been cancelled. No filesystem changes were made."
                conv_memory.add_message(owner_id, "assistant", cancel_msg)
                await send_formatted_message(update.effective_message, cancel_msg)
                return

            # Multiple pending: show selectable inline options
            lines = ["Multiple pending proposals found. Select one to cancel:\n"]
            buttons = []
            for wf in pending_wf:
                short_goal = wf["goal"][:25] + ("..." if len(wf["goal"]) > 25 else "")
                lines.append(f"• `{wf['goal']}`")
                buttons.append([InlineKeyboardButton(f"Cancel: {short_goal}", callback_data=f"wf_cancel:{wf['id']}")])

            for sa in pending_single:
                act_name = sa["action_type"].replace("_", " ").title()
                buttons.append([InlineKeyboardButton(f"Cancel: {act_name}", callback_data=f"cancel:{sa['id']}")])

            buttons.append([InlineKeyboardButton("Cancel All", callback_data="cancel_all_pending")])
            keyboard = InlineKeyboardMarkup(buttons)

            html_text = markdown_to_telegram_html("\n".join(lines))
            await update.effective_message.reply_text(html_text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    async def apps_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Display available approved Windows applications grouped by category."""
        if not is_authorized_owner(update, owner_id):
            if update.effective_message:
                await update.effective_message.reply_text(UNAUTHORIZED_MESSAGE)
            return

        if not update.effective_message:
            return

        approved = get_approved_apps()
        available_by_cat: dict[str, list[str]] = {}
        unavailable: list[str] = []

        for app_id, app in approved.items():
            is_avail, _, _ = is_app_available(app_id)
            if is_avail:
                available_by_cat.setdefault(app.category, []).append(app.display_name)
            else:
                unavailable.append(app.display_name)

        lines = ["**ORION — Available Applications**"]

        category_order = (
            "Productivity",
            "Development & AI",
            "Browsers & Communication",
            "Media & Utilities",
        )
        for cat in category_order:
            apps = available_by_cat.get(cat, [])
            if apps:
                lines.append(f"\n**{cat}:**")
                for name in apps:
                    lines.append(f"• {name}")

        if unavailable:
            lines.append("\n**Unavailable:**")
            for name in unavailable:
                lines.append(f"• {name}")

        await send_formatted_message(update.effective_message, "\n".join(lines))

    async def windows_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Display approved applications that currently have open desktop windows."""
        if not is_authorized_owner(update, owner_id):
            if update.effective_message:
                await update.effective_message.reply_text(UNAUTHORIZED_MESSAGE)
            return

        if not update.effective_message:
            return

        res = reg.execute("list_open_windows", {})
        reply_msg = format_tool_result("list_open_windows", res)
        await send_formatted_message(update.effective_message, reply_msg)

    async def active_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Display the currently active foreground approved application."""
        if not is_authorized_owner(update, owner_id):
            if update.effective_message:
                await update.effective_message.reply_text(UNAUTHORIZED_MESSAGE)
            return

        if not update.effective_message:
            return

        res = reg.execute("get_active_window", {})
        reply_msg = format_tool_result("get_active_window", res)
        await send_formatted_message(update.effective_message, reply_msg)

    async def process_user_text(user_text: str, reply_target: Any) -> None:
        async with user_lock:
            # Sensitive / Destructive operation guard for text commands
            destructive_keywords = {"delete", "remove", "erase", "overwrite", "wipe", "format", "destroy", "truncate"}
            words_in_text = set(re.findall(r"\b\w+\b", user_text.lower()))
            if bool(destructive_keywords & words_in_text) and any(
                w in words_in_text for w in ("that", "it", "this", "something", "everything", "all")
            ):
                clarif_msg = "Could you please clarify which file or action you want to perform?"
                await send_formatted_message(reply_target, clarif_msg)
                conv_memory.add_message(owner_id, "user", user_text)
                conv_memory.add_message(owner_id, "assistant", clarif_msg)
                return

            from orion.tools.desktop_inspector import (
                clear_pending_window_selection,
                create_window_tokens,
                is_pending_window_selection_active,
                resolve_choice_to_pending,
                resolve_window_token,
                _get_single_app_window,
                _verify_and_act_on_window,
            )

            # Check if user is replying with a choice to an active pending window selection
            if is_pending_window_selection_active():
                from orion.tools.desktop_inspector import get_latest_pending_selection, parse_window_choice

                idx = parse_window_choice(user_text)
                if idx is not None:
                    pending_info = resolve_choice_to_pending(user_text)
                    if not pending_info:
                        latest = get_latest_pending_selection()
                        num_opts = len(latest.get("tokens", {})) if latest else 2
                        approved_apps = get_approved_apps()
                        app_obj = approved_apps.get(latest.get("app_id", "")) if latest else None
                        app_name = app_obj.display_name if app_obj else "application"
                        invalid_msg = f"Please select a valid {app_name} window (1 to {num_opts})."
                        await send_formatted_message(reply_target, invalid_msg)
                        conv_memory.add_message(owner_id, "user", user_text)
                        conv_memory.add_message(owner_id, "assistant", invalid_msg)
                        return

                    app_id, action_type, token_id, label = pending_info
                    approved_apps = get_approved_apps()
                    app_obj = approved_apps.get(app_id)
                    app_name = app_obj.display_name if app_obj else app_id.title()

                    # 1. Revalidate the selected window before acting
                    win, err = resolve_window_token(token_id, app_id)
                    if err or win is None:
                        single_win, app_windows, _ = _get_single_app_window(app_id)
                        if app_windows:
                            options = create_window_tokens(app_id, app_windows, action=action_type)
                            buttons = []
                            count_str = "two" if len(app_windows) == 2 else str(len(app_windows))
                            sel_lines = [f"The selected window is no longer available. I found {count_str} open {app_name} windows. Which one?"]
                            for w in options:
                                b_idx = w["index"]
                                b_lbl = w.get("label") or f"{app_name} — Window {b_idx}"
                                b_tok = w["token_id"]
                                buttons.append([InlineKeyboardButton(f"{b_idx}. {b_lbl}", callback_data=f"win_sel:{b_tok}:{action_type}")])
                            kb = InlineKeyboardMarkup(buttons)
                            sel_reply = "\n".join(sel_lines)
                            try:
                                await reply_target.reply_text(markdown_to_telegram_html(sel_reply), parse_mode=ParseMode.HTML, reply_markup=kb)
                            except Exception:
                                await reply_target.reply_text(clean_markdown_to_plain(sel_reply), reply_markup=kb)
                            conv_memory.add_message(owner_id, "user", user_text)
                            conv_memory.add_message(owner_id, "assistant", sel_reply)
                        else:
                            clear_pending_window_selection()
                            msg = f"The selected window has closed, and {app_name} does not have any open windows."
                            await send_formatted_message(reply_target, msg)
                            conv_memory.add_message(owner_id, "user", user_text)
                            conv_memory.add_message(owner_id, "assistant", msg)
                        return

                    # 2. Resume the ORIGINAL pending operation after selection
                    act_res = _verify_and_act_on_window(win.hwnd, app_id, action_type)
                    tool_name = f"{action_type}_application_window"
                    is_successful = act_res.get("success", False)
                    status_str = "success" if is_successful else "failed"

                    conv_memory.log_action_history(
                        user_id=owner_id,
                        action_type=tool_name,
                        payload={"app_id": app_id, "window_token": token_id},
                        status=status_str,
                        result=act_res,
                    )

                    clear_pending_window_selection()

                    if is_successful:
                        result_msg = act_res.get("message", f"{label} updated successfully.")
                    else:
                        result_msg = act_res.get("error", "Failed to complete window operation.")

                    await send_formatted_message(reply_target, result_msg)
                    conv_memory.add_message(owner_id, "user", user_text)
                    conv_memory.add_message(owner_id, "assistant", result_msg)
                    return

            # 1. Attempt natural-language tool routing first
            tool_res = await route_and_execute_tool(
                user_text,
                ollama_client=client,
                registry=reg,
                context={"user_id": owner_id, "owner_id": owner_id, "memory": conv_memory},
            )
            if tool_res and tool_res.get("handled"):
                if tool_res.get("requires_selection"):
                    windows = tool_res.get("windows", [])
                    disp = tool_res.get("display_name") or tool_res.get("app_id", "Application").title()
                    act_type = tool_res.get("action_type", "focus")
                    buttons = []
                    for w in windows:
                        b_idx = w["index"]
                        b_lbl = w.get("label") or f"{disp} — Window {b_idx}"
                        b_tok = w["token_id"]
                        buttons.append([InlineKeyboardButton(f"{b_idx}. {b_lbl}", callback_data=f"win_sel:{b_tok}:{act_type}")])
                    sel_keyboard = InlineKeyboardMarkup(buttons)
                    reply_text = tool_res.get("reply", f"Which {disp} window?")
                    try:
                        await reply_target.reply_text(markdown_to_telegram_html(reply_text), parse_mode=ParseMode.HTML, reply_markup=sel_keyboard)
                    except Exception:
                        await reply_target.reply_text(clean_markdown_to_plain(reply_text), reply_markup=sel_keyboard)
                    conv_memory.add_message(owner_id, "user", user_text)
                    conv_memory.add_message(owner_id, "assistant", reply_text)
                    return

                if tool_res.get("is_workflow_proposal"):
                    workflow_id = conv_memory.create_execution_workflow(
                        user_id=owner_id,
                        goal=tool_res["goal"],
                        steps=tool_res["steps"],
                        ttl_seconds=300,
                    )
                    proposal_text = tool_res.get("proposal_text", "")
                    keyboard = InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton("Approve All", callback_data=f"wf_approve:{workflow_id}"),
                                InlineKeyboardButton("Cancel", callback_data=f"wf_cancel:{workflow_id}"),
                            ]
                        ]
                    )
                    html_msg = markdown_to_telegram_html(proposal_text)
                    try:
                        await reply_target.reply_text(
                            html_msg,
                            parse_mode=ParseMode.HTML,
                            reply_markup=keyboard,
                        )
                    except Exception:
                        await reply_target.reply_text(
                            clean_markdown_to_plain(proposal_text),
                            reply_markup=keyboard,
                        )
                    conv_memory.add_message(owner_id, "user", user_text)
                    conv_memory.add_message(owner_id, "assistant", proposal_text)
                    return

                if tool_res.get("is_proposal"):
                    action_id = conv_memory.create_pending_action(
                        user_id=owner_id,
                        action_type=tool_res["action_type"],
                        payload=tool_res["arguments"],
                        ttl_seconds=300,
                    )
                    proposal_text = tool_res.get("proposal_text", "")
                    keyboard = InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton("Confirm", callback_data=f"confirm:{action_id}"),
                                InlineKeyboardButton("Cancel", callback_data=f"cancel:{action_id}"),
                            ]
                        ]
                    )
                    html_msg = markdown_to_telegram_html(proposal_text)
                    try:
                        await reply_target.reply_text(
                            html_msg,
                            parse_mode=ParseMode.HTML,
                            reply_markup=keyboard,
                        )
                    except Exception:
                        await reply_target.reply_text(
                            clean_markdown_to_plain(proposal_text),
                            reply_markup=keyboard,
                        )
                    conv_memory.add_message(owner_id, "user", user_text)
                    conv_memory.add_message(owner_id, "assistant", proposal_text)
                    return

                reply_text = tool_res.get("reply", "")
                conv_memory.add_message(owner_id, "user", user_text)
                conv_memory.add_message(owner_id, "assistant", reply_text)
            else:
                # 2. Standard conversational flow with persistent memory
                history = conv_memory.get_recent_messages(
                    user_id=owner_id,
                    limit=settings.max_history_messages,
                )
                reply_text = await client.generate_response(user_text, history=history)

                if reply_text != OLLAMA_UNAVAILABLE_MESSAGE:
                    conv_memory.add_message(owner_id, "user", user_text)
                    conv_memory.add_message(owner_id, "assistant", reply_text)

        await send_formatted_message(reply_target, reply_text)

    async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized_owner(update, owner_id):
            if update.effective_message:
                await update.effective_message.reply_text(UNAUTHORIZED_MESSAGE)
            return

        if not update.effective_message or not update.effective_message.text:
            return

        user_text = update.effective_message.text.strip()
        if not user_text:
            return

        if update.effective_chat:
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id,
                action=ChatAction.TYPING,
            )

        await process_user_text(user_text, update.effective_message)

    async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming Telegram voice notes with offline speech recognition."""
        if not is_authorized_owner(update, owner_id):
            if update.effective_message:
                await update.effective_message.reply_text(UNAUTHORIZED_MESSAGE)
            return

        if not update.effective_message or not update.effective_message.voice:
            return

        voice = update.effective_message.voice

        if not settings.voice_enabled or voice_transcriber is None:
            await send_formatted_message(
                update.effective_message,
                "Voice commands are currently disabled in ORION configuration.",
            )
            return

        if voice.duration and voice.duration > settings.max_voice_duration_seconds:
            await send_formatted_message(
                update.effective_message,
                f"Voice notes longer than {settings.max_voice_duration_seconds} seconds are not supported.",
            )
            return

        if voice.file_size and voice.file_size > settings.max_voice_file_size_bytes:
            await send_formatted_message(
                update.effective_message,
                "Voice note exceeds the maximum allowed file size.",
            )
            return

        if update.effective_chat:
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id,
                action=ChatAction.TYPING,
            )

        import os
        import tempfile

        temp_audio_file = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
        temp_audio_path = temp_audio_file.name
        temp_audio_file.close()

        try:
            voice_file = await context.bot.get_file(voice.file_id)
            await voice_file.download_to_drive(temp_audio_path)
            transcribed_text = await voice_transcriber.transcribe_async(temp_audio_path)
        except asyncio.TimeoutError:
            logger.warning("Voice transcription timed out.")
            await send_formatted_message(
                update.effective_message,
                "Voice processing timed out. Please try speaking again with a shorter message or send text.",
            )
            return
        except Exception as err:
            logger.error("Error transcribing voice note: %s", err)
            await send_formatted_message(
                update.effective_message,
                "Unable to process voice message. Please try speaking again or send a text message.",
            )
            return
        finally:
            try:
                if os.path.exists(temp_audio_path):
                    os.remove(temp_audio_path)
            except Exception:
                pass

        raw_transcript = (transcribed_text or "").strip()
        logger.info("Voice transcription received (raw): %r", raw_transcript)

        # Filter out Whisper non-speech artifacts: e.g. "[BLANK_AUDIO]", "[MUSIC]", "...", "(laughter)"
        clean_speech = re.sub(r"\[.*?\]|\(.*?\)", "", raw_transcript).strip(" .!?\"'-")
        if not clean_speech or not re.search(r"\w", clean_speech):
            await send_formatted_message(
                update.effective_message,
                "I couldn't clearly understand that voice message. Please try speaking again or send a text message.",
            )
            return

        interpreted_text, is_ambiguous, clarification = interpret_voice_command(
            clean_speech,
            recent_context={"owner_id": owner_id, "memory": conv_memory},
        )

        if is_ambiguous and clarification:
            await send_formatted_message(update.effective_message, clarification)
            return

        clean_text = (interpreted_text or "").strip()
        if not clean_text:
            await send_formatted_message(
                update.effective_message,
                "I couldn't clearly understand that voice message. Please try speaking again or send a text message.",
            )
            return

        await process_user_text(clean_text, update.effective_message)

    async def confirmation_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle inline button confirmation or cancellation for pending file actions."""
        query = update.callback_query
        if not query:
            return

        # Restrict confirmation callback strictly to authorized owner
        if not update.effective_user or update.effective_user.id != owner_id:
            await query.answer(UNAUTHORIZED_MESSAGE, show_alert=True)
            return

        await query.answer()

        data = query.data or ""
        if data == "cancel_all_pending":
            async with user_lock:
                from orion.tools.desktop_inspector import clear_pending_window_selection
                clear_pending_window_selection()
                cancelled_count = conv_memory.cancel_all_pending(owner_id)
                msg = f"Cancelled {cancelled_count} pending proposal(s). No filesystem changes were made."
                try:
                    await query.edit_message_text(msg)
                except Exception:
                    pass
                conv_memory.add_message(owner_id, "assistant", msg)
            return

        if ":" not in data:
            return
        action, action_id = data.split(":", 1)

        async with user_lock:
            # Handle multi-step execution workflows (Step 7)
            if action == "wf_cancel":
                from orion.tools.desktop_inspector import clear_pending_window_selection
                clear_pending_window_selection()
                success = conv_memory.cancel_execution_workflow(action_id)
                if success:
                    cancel_msg = "Task cancelled. No filesystem changes were made."
                    try:
                        await query.edit_message_text(cancel_msg)
                    except Exception:
                        pass
                    conv_memory.add_message(owner_id, "assistant", cancel_msg)
                else:
                    await query.edit_message_text(
                        "This workflow is no longer active (already completed, cancelled, or expired)."
                    )
                return

            if action == "wf_approve":
                workflow = conv_memory.get_execution_workflow(action_id, user_id=owner_id)
                if not workflow:
                    await query.edit_message_text("Workflow not found.")
                    return

                if workflow["status"] != "pending":
                    msg = (
                        "⏱️ This task proposal has expired. No changes were made."
                        if workflow["status"] == "expired"
                        else "⚠️ This workflow was already processed."
                    )
                    await query.edit_message_text(msg)
                    return

                # Atomically claim workflow to prevent duplicate or concurrent execution
                claimed = conv_memory.claim_execution_workflow(action_id)
                if not claimed:
                    await query.edit_message_text("⚠️ This workflow was already processed.")
                    return

                # Acknowledge execution start
                try:
                    await query.edit_message_text("Working on your request...")
                except Exception:
                    pass

                from orion.core.execution_engine import execute_workflow

                res = await execute_workflow(
                    workflow_id=action_id,
                    steps=workflow["steps"],
                    user_id=owner_id,
                    memory=conv_memory,
                    registry=reg,
                )

                result_msg = res.get("message", "Workflow finished.")
                html_res = markdown_to_telegram_html(result_msg)
                try:
                    await query.edit_message_text(html_res, parse_mode=ParseMode.HTML)
                except Exception:
                    await query.edit_message_text(clean_markdown_to_plain(result_msg))

                conv_memory.add_message(owner_id, "assistant", result_msg)
                return

            # Handle single-action file operations (Step 6)
            if action == "cancel":
                from orion.tools.desktop_inspector import clear_pending_window_selection
                clear_pending_window_selection()
                success = conv_memory.mark_pending_action_status(
                    action_id,
                    from_status="pending",
                    to_status="cancelled",
                )
                if success:
                    act = conv_memory.get_pending_action(action_id, user_id=owner_id)
                    if act:
                        conv_memory.log_action_history(
                            user_id=owner_id,
                            action_type=act["action_type"],
                            payload=act["payload"],
                            status="cancelled",
                        )
                    if act and act.get("action_type") == "launch_application":
                        cancel_msg = "❌ Operation cancelled. No application was launched."
                    elif act and act.get("action_type") in _WINDOW_MANAGEMENT_ACTION_TYPES:
                        cancel_msg = "❌ Pending window operation cancelled. No changes were made."
                    elif act and act.get("action_type") == "composite_app_action":
                        cancel_msg = "❌ Operation cancelled. No applications were launched."
                    else:
                        cancel_msg = "❌ Operation cancelled. No filesystem changes were made."
                    html_cancel = markdown_to_telegram_html(cancel_msg)
                    try:
                        await query.edit_message_text(html_cancel, parse_mode=ParseMode.HTML)
                    except Exception:
                        await query.edit_message_text(clean_markdown_to_plain(cancel_msg))
                    conv_memory.add_message(owner_id, "assistant", cancel_msg)
                else:
                    await query.edit_message_text(
                        "This action is no longer active (already completed, cancelled, or expired)."
                    )
                return

            if action == "confirm":
                act = conv_memory.get_pending_action(action_id, user_id=owner_id)
                if not act:
                    await query.edit_message_text("Action not found.")
                    return

                if act["status"] != "pending":
                    msg = (
                        "⏱️ This action has expired. No changes were made."
                        if act["status"] == "expired"
                        else "This action was already processed."
                    )
                    await query.edit_message_text(msg)
                    return

                # Atomically claim action to prevent duplicate execution
                success = conv_memory.mark_pending_action_status(
                    action_id,
                    from_status="pending",
                    to_status="confirmed",
                )
                if not success:
                    await query.edit_message_text("This action was already processed.")
                    return

                # Re-validate permissions, filesystem state, or application availability prior to execution
                if act["action_type"] == "launch_application":
                    app_id = act["payload"].get("app_id", "")
                    is_avail, avail_err, _ = is_app_available(app_id)
                    if not is_avail:
                        app_obj = get_approved_apps().get(app_id)
                        name = app_obj.display_name if app_obj else app_id
                        fail_msg = f"{name} — Failed to launch: Application is not currently installed or available."
                        conv_memory.log_action_history(
                            user_id=owner_id,
                            action_type=act["action_type"],
                            payload=act["payload"],
                            status="failed",
                            result={"success": False, "error": avail_err},
                        )
                        html_fail = markdown_to_telegram_html(fail_msg)
                        try:
                            await query.edit_message_text(html_fail, parse_mode=ParseMode.HTML)
                        except Exception:
                            await query.edit_message_text(clean_markdown_to_plain(fail_msg))
                        conv_memory.add_message(owner_id, "assistant", fail_msg)
                        return

                exec_result = reg.execute(act["action_type"], act["payload"])
                tool_data = exec_result.get("data", {})

                # Handle requires_selection for window management (multi-window case)
                if act["action_type"] in _WINDOW_MANAGEMENT_ACTION_TYPES and tool_data.get("requires_selection"):
                    windows = tool_data.get("windows", [])
                    disp = tool_data.get("display_name", act["payload"].get("app_id", "Application"))
                    if windows:
                        count_str = "two" if len(windows) == 2 else str(len(windows))
                        sel_lines = [f"I found {count_str} {disp} windows. Which one?"]
                        buttons = []
                        for idx, w in enumerate(windows, start=1):
                            tok_id = w["token_id"]
                            label = w.get("label") or f"{disp} — Window {idx}"
                            sel_lines.append(f"{idx}. {label}")
                            # callback_data must be ≤ 64 bytes: "win_sel:<tok_id>:<action_type>"
                            cb = f"win_sel:{tok_id}:{act['action_type']}"
                            buttons.append([InlineKeyboardButton(f"{idx}. {label}", callback_data=cb)])
                        sel_keyboard = InlineKeyboardMarkup(buttons)
                        sel_text = "\n".join(sel_lines)
                        try:
                            await query.edit_message_text(sel_text, parse_mode=ParseMode.HTML, reply_markup=sel_keyboard)
                        except Exception:
                            await query.edit_message_text(clean_markdown_to_plain(sel_text), reply_markup=sel_keyboard)
                    else:
                        await query.edit_message_text(f"Window selection failed: no options available.")
                    return

                is_successful = (
                    exec_result.get("success", False)
                    and (not isinstance(tool_data, dict) or tool_data.get("success", True) is not False)
                )
                status_str = "success" if is_successful else "failed"

                conv_memory.log_action_history(
                    user_id=owner_id,
                    action_type=act["action_type"],
                    payload=act["payload"],
                    status=status_str,
                    result=exec_result,
                )

                result_msg = format_tool_result(act["action_type"], exec_result)
                html_res = markdown_to_telegram_html(result_msg)
                try:
                    await query.edit_message_text(html_res, parse_mode=ParseMode.HTML)
                except Exception:
                    await query.edit_message_text(clean_markdown_to_plain(result_msg))

                conv_memory.add_message(owner_id, "assistant", result_msg)

            # Handle multi-window selection callbacks: "win_sel:<token_id>:<action_type>"
            if action == "win_sel":
                parts = action_id.split(":", 1)
                if len(parts) != 2:
                    await query.answer("Invalid window selection.", show_alert=True)
                    return
                token_id, win_action_type = parts
                if win_action_type not in _WINDOW_MANAGEMENT_ACTION_TYPES and f"{win_action_type}_application_window" not in _WINDOW_MANAGEMENT_ACTION_TYPES:
                    await query.answer("Invalid window action type.", show_alert=True)
                    return

                from orion.tools.desktop_inspector import (
                    _window_tokens,
                    resolve_window_token,
                    _verify_and_act_on_window,
                    clear_pending_window_selection,
                    _get_single_app_window,
                    create_window_tokens,
                )
                tok = _window_tokens.get(token_id)
                if not tok or tok.is_expired():
                    clear_pending_window_selection()
                    await query.edit_message_text("Window selection has expired. Please retry the operation.")
                    return

                app_id = tok.app_id
                action_name = win_action_type.replace("_application_window", "")
                norm_tool_name = f"{action_name}_application_window"

                # 1. Revalidate the selected window before acting
                win, err = resolve_window_token(token_id, app_id)
                if err or win is None:
                    single_win, app_windows, _ = _get_single_app_window(app_id)
                    disp = get_approved_apps().get(app_id)
                    app_name = disp.display_name if disp else app_id.title()
                    if app_windows:
                        options = create_window_tokens(app_id, app_windows, action=action_name)
                        buttons = []
                        count_str = "two" if len(app_windows) == 2 else str(len(app_windows))
                        sel_lines = [f"The selected window is no longer available. I found {count_str} open {app_name} windows. Which one?"]
                        for w in options:
                            b_idx = w["index"]
                            b_lbl = w.get("label") or f"{app_name} — Window {b_idx}"
                            b_tok = w["token_id"]
                            buttons.append([InlineKeyboardButton(f"{b_idx}. {b_lbl}", callback_data=f"win_sel:{b_tok}:{action_name}")])
                        kb = InlineKeyboardMarkup(buttons)
                        await query.edit_message_text("\n".join(sel_lines), reply_markup=kb)
                    else:
                        clear_pending_window_selection()
                        await query.edit_message_text(f"The selected window has closed, and {app_name} does not have any open windows.")
                    return

                # 2. Resume the ORIGINAL pending operation
                act_res = _verify_and_act_on_window(win.hwnd, app_id, action_name)
                is_successful = act_res.get("success", False)
                status_str = "success" if is_successful else "failed"

                conv_memory.log_action_history(
                    user_id=owner_id,
                    action_type=norm_tool_name,
                    payload={"app_id": app_id, "window_token": token_id},
                    status=status_str,
                    result=act_res,
                )

                clear_pending_window_selection()

                if is_successful:
                    result_msg = act_res.get("message", f"{tok.label} updated successfully.")
                else:
                    result_msg = act_res.get("error", "Failed to complete window operation.")

                try:
                    await query.edit_message_text(markdown_to_telegram_html(result_msg), parse_mode=ParseMode.HTML)
                except Exception:
                    await query.edit_message_text(clean_markdown_to_plain(result_msg))

                conv_memory.add_message(owner_id, "assistant", result_msg)
                return

    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle errors cleanly without printing stack traces to chat or stdout."""
        if isinstance(update, Update) and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "I encountered an unexpected issue processing that. Please try again."
                )
            except Exception:
                pass

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("reset", reset_handler))
    app.add_handler(CommandHandler("plan", plan_handler))
    app.add_handler(CommandHandler("tasks", tasks_handler))
    app.add_handler(CommandHandler("task", task_handler))
    app.add_handler(CommandHandler(["workflow", "run"], workflow_handler))
    app.add_handler(CommandHandler("status", status_handler))
    app.add_handler(CommandHandler("history", history_handler))
    app.add_handler(CommandHandler("cancel", cancel_handler))
    app.add_handler(CommandHandler("apps", apps_handler))
    app.add_handler(CommandHandler("windows", windows_handler))
    app.add_handler(CommandHandler("active", active_handler))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CallbackQueryHandler(confirmation_callback_handler))
    app.add_handler(MessageHandler(filters.VOICE, voice_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.add_error_handler(error_handler)

    return app
