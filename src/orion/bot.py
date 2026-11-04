"""Runner for launching the ORION Telegram bot and FastAPI health server."""

import sys
import uvicorn
from orion.config.settings import get_settings


def start() -> None:
    """Validate settings and launch ORION services."""
    settings = get_settings()
    try:
        settings.validate_telegram_configuration()
    except ValueError as err:
        print(f"\n[ORION Configuration Error]\n{err}\n", file=sys.stderr)
        sys.exit(1)

    print(
        f"Starting ORION on http://{settings.host}:{settings.port} "
        "(Telegram long-polling enabled)..."
    )
    uvicorn.run("orion.main:app", host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    start()
