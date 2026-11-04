"""Integrations module for external local clients and services."""

from orion.integrations.formatter import markdown_to_telegram_html
from orion.integrations.ollama import OllamaClient
from orion.integrations.telegram_bot import create_telegram_app, send_formatted_message

__all__ = [
    "OllamaClient",
    "create_telegram_app",
    "markdown_to_telegram_html",
    "send_formatted_message",
]
