"""Integrations module for external local clients and services."""

from orion.integrations.ollama import OllamaClient
from orion.integrations.telegram_bot import create_telegram_app

__all__ = ["OllamaClient", "create_telegram_app"]
