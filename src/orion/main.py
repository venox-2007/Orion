"""FastAPI application entrypoint for ORION."""

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from orion.config.settings import get_settings
from orion.integrations.telegram_bot import create_telegram_app


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage application lifecycle, optionally starting Telegram polling."""
    settings = get_settings()
    telegram_app = None

    if settings.telegram_bot_token and settings.telegram_owner_id:
        try:
            telegram_app = create_telegram_app(
                token=settings.telegram_bot_token,
                owner_id=settings.telegram_owner_id,
            )
            await telegram_app.initialize()
            await telegram_app.start()
            await telegram_app.updater.start_polling()
        except Exception:
            pass

    yield

    if telegram_app and telegram_app.updater and telegram_app.updater.running:
        try:
            await telegram_app.updater.stop()
            await telegram_app.stop()
            await telegram_app.shutdown()
        except Exception:
            pass


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
