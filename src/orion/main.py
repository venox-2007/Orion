"""FastAPI application entrypoint for ORION."""

from contextlib import asynccontextmanager
import logging
from typing import AsyncIterator

from fastapi import FastAPI
from orion.config.settings import get_settings
from orion.integrations.telegram_bot import create_telegram_app

logger = logging.getLogger("orion.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage application lifecycle, starting Telegram polling and reminder scheduler."""
    settings = get_settings()
    telegram_app = None

    if settings.telegram_bot_token and settings.telegram_owner_id:
        try:
            telegram_app = create_telegram_app(
                token=settings.telegram_bot_token,
                owner_id=settings.telegram_owner_id,
            )
            await telegram_app.initialize()

            # Explicitly invoke post_init callback (starts reminder scheduler)
            if telegram_app.post_init:
                await telegram_app.post_init(telegram_app)

            await telegram_app.start()

            if telegram_app.updater:
                await telegram_app.updater.start_polling()

            logger.info("Telegram bot and reminder scheduler initialized and running.")
        except Exception as err:
            logger.error("Failed to start Telegram bot service: %s", err)
            # Safe cleanup of partially initialized components
            if telegram_app:
                try:
                    if telegram_app.updater and telegram_app.updater.running:
                        await telegram_app.updater.stop()
                    await telegram_app.stop()
                    if telegram_app.post_shutdown:
                        await telegram_app.post_shutdown(telegram_app)
                    await telegram_app.shutdown()
                except Exception as cleanup_err:
                    logger.error("Error during partial startup cleanup: %s", cleanup_err)
                telegram_app = None

    # Wire live two-way voice loop listener (Step 34)
    voice_manager = None
    if settings.voice_autostart:
        try:
            from orion.voice.stt import get_voice_loop_manager

            voice_manager = get_voice_loop_manager()
            await voice_manager.start()
            logger.info("VoiceLoopManager autostarted.")
        except Exception as v_err:
            logger.warning("Failed to autostart VoiceLoopManager: %s", v_err)

    yield

    if voice_manager:
        try:
            await voice_manager.stop()
        except Exception as v_err:
            logger.debug("Error stopping VoiceLoopManager: %s", v_err)

    if telegram_app:
        try:
            if telegram_app.updater and telegram_app.updater.running:
                await telegram_app.updater.stop()
            await telegram_app.stop()

            # Explicitly invoke post_shutdown callback (stops reminder scheduler)
            if telegram_app.post_shutdown:
                await telegram_app.post_shutdown(telegram_app)

            await telegram_app.shutdown()
            logger.info("Telegram bot and reminder scheduler shut down cleanly.")
        except Exception as err:
            logger.error("Error during Telegram bot shutdown: %s", err)


settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    debug=settings.debug,
    lifespan=lifespan,
)


@app.get("/health")
def health_check() -> dict[str, str]:
    """Health check endpoint returning service status."""
    return {"status": "healthy"}
