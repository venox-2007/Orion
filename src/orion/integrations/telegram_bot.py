"""Telegram bot integration for ORION using long polling."""

import asyncio
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
    "• `/reset` — Clear conversation history"
)
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


def create_telegram_app(
    token: str,
    owner_id: int,
    ollama_client: OllamaClient | None = None,
    memory: ConversationMemory | None = None,
    tool_registry: ToolRegistry | None = None,
) -> Application:
    """Build and configure the Telegram application with handlers."""
    client = ollama_client or OllamaClient()
    conv_memory = memory or ConversationMemory()
    reg = tool_registry or default_tool_registry
    settings = get_settings()
    user_lock = asyncio.Lock()

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

        async with user_lock:
            # 1. Attempt natural-language tool routing first
            tool_res = await route_and_execute_tool(user_text, ollama_client=client, registry=reg)
            if tool_res and tool_res.get("handled"):
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

        await send_formatted_message(update.effective_message, reply_text)

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
                status_str = "success" if exec_result.get("success") else "failed"

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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.add_error_handler(error_handler)

    return app
