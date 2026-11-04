"""Telegram bot integration for ORION using long polling."""

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from orion.integrations.ollama import OllamaClient

UNAUTHORIZED_MESSAGE = "Access restricted: unauthorized user or chat."
WELCOME_MESSAGE = "Hello! I am ORION, your personal AI assistant. How can I help you today?"


def split_message(text: str, max_chunk: int = 4000) -> list[str]:
    """Split response into chunks safe for Telegram's 4096-character limit."""
    if len(text) <= max_chunk:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_chunk:
            chunks.append(remaining)
            break

        split_point = remaining.rfind("\n", 0, max_chunk)
        if split_point == -1 or split_point == 0:
            split_point = remaining.rfind(" ", 0, max_chunk)
        if split_point == -1 or split_point == 0:
            split_point = max_chunk

        chunk = remaining[:split_point].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_point:].strip()

    return chunks or [text[:max_chunk]]


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
) -> Application:
    """Build and configure the Telegram application with handlers."""
    client = ollama_client or OllamaClient()

    async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized_owner(update, owner_id):
            if update.effective_message:
                await update.effective_message.reply_text(UNAUTHORIZED_MESSAGE)
            return

        if update.effective_message:
            await update.effective_message.reply_text(WELCOME_MESSAGE)

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

        reply_text = await client.generate_response(user_text)
        for chunk in split_message(reply_text):
            await update.effective_message.reply_text(chunk)

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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.add_error_handler(error_handler)

    return app
