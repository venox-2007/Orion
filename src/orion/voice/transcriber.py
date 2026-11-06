"""Local offline speech-to-text transcriber for ORION voice commands (Step 12).

Uses faster-whisper on CPU int8 by default to avoid competing with Ollama for GPU VRAM.
Lazy-loads model weights on first voice note demand, caches models outside Git, and runs
transcription asynchronously without blocking the Telegram event loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

logger = logging.getLogger("orion.voice.transcriber")


class VoiceTranscriber:
    """Offline speech-to-text transcriber using faster-whisper."""

    def __init__(
        self,
        model_size: str = "base",
        device: str = "cpu",
        compute_type: str = "int8",
        download_root: str | None = None,
    ) -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.download_root = download_root
        self._model: WhisperModel | None = None
        self._lock = threading.Lock()
        self._async_lock = asyncio.Lock()

    def is_loaded(self) -> bool:
        """Check if the Whisper model is currently loaded into memory."""
        return self._model is not None

    def get_model(self) -> WhisperModel:
        """Lazily initialize and return the cached WhisperModel."""
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from faster_whisper import WhisperModel

                    logger.info(
                        "Loading faster-whisper model '%s' (device=%s, compute_type=%s)...",
                        self.model_size,
                        self.device,
                        self.compute_type,
                    )
                    self._model = WhisperModel(
                        self.model_size,
                        device=self.device,
                        compute_type=self.compute_type,
                        download_root=self.download_root,
                    )
                    logger.info("faster-whisper model '%s' loaded successfully.", self.model_size)
        return self._model

    def transcribe(self, audio_path: str | Path) -> str:
        """Transcribe an audio file synchronously.

        Returns combined recognized text stripped of whitespace,
        or empty string if speech is silent, unclear, or empty.
        """
        path_str = str(audio_path)
        if not os.path.exists(path_str):
            raise FileNotFoundError(f"Audio file not found: {path_str}")

        if os.path.getsize(path_str) == 0:
            logger.warning("Audio file is 0 bytes; returning empty transcription.")
            return ""

        model = self.get_model()
        try:
            # Beam size 5 provides high accuracy for multilingual speech (English / Hindi-English)
            segments, info = model.transcribe(
                path_str,
                beam_size=5,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=500),
            )
            transcription_parts = [seg.text.strip() for seg in segments if seg.text.strip()]
            return " ".join(transcription_parts).strip()
        except Exception as err:
            logger.error("Speech transcription error: %s", err)
            raise

    async def transcribe_async(self, audio_path: str | Path, timeout: float = 30.0) -> str:
        """Transcribe an audio file asynchronously using bounded concurrency.

        Ensures only one transcription runs at a time and executes on a background
        worker thread without blocking Telegram's event loop, guarded by a timeout.
        """
        async with self._async_lock:
            return await asyncio.wait_for(
                asyncio.to_thread(self.transcribe, audio_path),
                timeout=timeout,
            )
