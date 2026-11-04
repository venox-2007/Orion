"""Integrations module for external local clients and services."""

from orion.integrations.formatter import markdown_to_telegram_html
from orion.integrations.ollama import OllamaClient

__all__ = [
    "OllamaClient",
    "markdown_to_telegram_html",
]
