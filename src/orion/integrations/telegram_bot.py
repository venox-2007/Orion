"""Telegram bot integration for ORION using long polling."""

import asyncio
from typing import Any
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
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
from orion.tools.registry import ToolRegistry, default_tool_registry
from orion.tools.router import route_and_execute_tool

UNAUTHORIZED_MESSAGE = "Access restricted: unauthorized user or chat."
WELCOME_MESSAGE = "Hello! I am ORION, your personal AI assistant. How can I help you today?"
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.add_error_handler(error_handler)

    return app
