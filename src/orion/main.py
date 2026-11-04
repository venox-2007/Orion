"""FastAPI application entrypoint for ORION."""

from fastapi import FastAPI
from orion.config.settings import get_settings

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    debug=settings.debug,
)


@app.get("/health")
def health_check() -> dict[str, str]:
    """Health check endpoint returning service status."""
    return {"status": "healthy"}
