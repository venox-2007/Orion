"""Ollama integration client for local LLM inference."""

import re
import httpx
from orion.config.settings import get_settings

DEFAULT_SYSTEM_PROMPT = (
    "You are ORION, a private, local-first personal AI assistant. "
    "You communicate clearly, adaptively, and concisely. "
    "You are helpful and speak naturally. "
    "You only assist with text conversation and do not claim to have physical, "
    "voice, browser, or external tool execution capabilities."
)

OLLAMA_UNAVAILABLE_MESSAGE = (
    "I'm having trouble connecting to my local language model right now. "
    "Please make sure Ollama is running locally."
)


def clean_model_reply(raw_text: str) -> str:
    """Strip reasoning/thinking traces or metadata from model output."""
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", raw_text, flags=re.IGNORECASE)
    return cleaned.strip()


class OllamaClient:
    """Minimal client for communicating with local Ollama API."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.model = model or settings.ollama_model
        self.timeout = timeout if timeout is not None else settings.ollama_timeout

    async def generate_response(
        self,
        user_message: str,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    ) -> str:
        """Send chat messages to the local Ollama API and return a clean response."""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "stream": False,
            "think": False,
            "keep_alive": "30m",
            "options": {"num_predict": 256},
        }

        url = f"{self.base_url}/api/chat"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, json=payload)
                if response.status_code != 200:
                    return OLLAMA_UNAVAILABLE_MESSAGE
                data = response.json()
                raw_content = data.get("message", {}).get("content", "")
                cleaned = clean_model_reply(raw_content)
                return cleaned if cleaned else OLLAMA_UNAVAILABLE_MESSAGE
        except Exception:
            return OLLAMA_UNAVAILABLE_MESSAGE
