"""Local Speech-to-Text (STT) listener and wake-word voice loop for ORION (Step 34)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import re
from typing import Any, Callable

from orion.voice import tts

logger = logging.getLogger("orion.voice.stt")

_SPEECH_REC_SCRIPT = Path(__file__).parent / "speech_rec.ps1"


async def listen_once(timeout_sec: float = 6.0) -> str | None:
    """Listen to default microphone for speech and return recognized text, or None."""
    if not _SPEECH_REC_SCRIPT.exists():
        logger.error("speech_rec.ps1 not found at %s", _SPEECH_REC_SCRIPT)
        return None

    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(_SPEECH_REC_SCRIPT),
            "-TimeoutSec",
            str(timeout_sec),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec + 4.0)
        text = stdout.decode("utf-8", errors="replace").strip()
        return text if text else None
    except Exception as err:
        logger.debug("STT listen_once error: %s", err)
        return None


def extract_wake_command(raw_text: str, wake_word: str = "orion") -> tuple[bool, str | None]:
    """Check if raw_text contains the wake-word and extract the subsequent command.

    Returns:
        (has_wake_word, command_part)
        - If bare wake word was uttered without command, returns (True, None).
        - If wake word was uttered with command, returns (True, "command string").
        - If wake word was not present, returns (False, None).
    """
    clean = (raw_text or "").strip()
    if not clean:
        return False, None

    # Match: "orion", "hey orion", "ok orion", "hi orion" followed by optional command
    pattern = rf"^(?:hey\s+|hi\s+|ok\s+)?{re.escape(wake_word)}[,\s]*(.*)$"
    m = re.match(pattern, clean, re.IGNORECASE)
    if not m:
        return False, None

    cmd = m.group(1).strip(" ,.!?\"'")
    return True, (cmd if cmd else None)


class VoiceLoopManager:
    """Continuous wake-word and command listening loop in background."""

    def __init__(
        self,
        wake_word: str = "orion",
        listen_fn: Callable[..., Any] | None = None,
        speak_fn: Callable[..., Any] | None = None,
        router_fn: Callable[..., Any] | None = None,
    ) -> None:
        self.wake_word = wake_word
        self._listen_fn = listen_fn or listen_once
        self._speak_fn = speak_fn or tts.speak_text
        self._router_fn = router_fn
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        """Check if voice loop task is active."""
        return self._running and self._task is not None and not self._task.done()

    async def start(self) -> bool:
        """Start background voice listener loop."""
        async with self._lock:
            if self.is_running:
                return True
            self._running = True
            self._task = asyncio.create_task(self._loop())
            logger.info("VoiceLoopManager started (wake word: '%s').", self.wake_word)
            return True

    async def stop(self) -> None:
        """Stop background voice listener loop and halt speech."""
        async with self._lock:
            self._running = False
            if self._task and not self._task.done():
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            self._task = None
            tts.stop_speaking()
            logger.info("VoiceLoopManager stopped.")

    def status(self) -> dict[str, Any]:
        """Return status dictionary of voice loop manager."""
        return {
            "active": self.is_running,
            "wake_word": self.wake_word,
            "is_speaking": tts.is_speaking(),
        }

    async def _loop(self) -> None:
        """Background listening loop."""
        if self._router_fn is None:
            from orion.tools.router import route_and_execute_tool

            self._router_fn = route_and_execute_tool

        while self._running:
            try:
                # 1. Listen for wake word or complete command
                speech = await self._listen_fn(timeout_sec=4.0)
                if not self._running:
                    break
                if not speech:
                    await asyncio.sleep(0.3)
                    continue

                logger.debug("Captured speech: '%s'", speech)
                has_wake, cmd = extract_wake_command(speech, self.wake_word)
                if not has_wake:
                    await asyncio.sleep(0.2)
                    continue

                # 2. If bare wake-word was uttered without command, prompt and listen for follow-up
                if not cmd:
                    await self._speak_fn("Yes?")
                    follow_up = await self._listen_fn(timeout_sec=6.0)
                    if not follow_up or not self._running:
                        continue
                    cmd = follow_up.strip(" ,.!?\"'")

                if not cmd or not self._running:
                    continue

                logger.info("Voice command triggered: '%s'", cmd)

                # 3. Route command through ORION
                res = await self._router_fn(cmd, context={"_voice": True, "source": "voice"})
                if res and res.get("handled"):
                    reply = res.get("reply", "Executed.")
                else:
                    reply = f"Sorry, I did not understand {cmd}."

                # 4. Speak response aloud
                if self._running:
                    await self._speak_fn(reply)

            except asyncio.CancelledError:
                break
            except Exception as loop_err:
                logger.error("VoiceLoop error: %s", loop_err)
                await asyncio.sleep(1.0)


_VOICE_MANAGER: VoiceLoopManager | None = None


def get_voice_loop_manager() -> VoiceLoopManager:
    """Return singleton VoiceLoopManager instance."""
    global _VOICE_MANAGER
    if _VOICE_MANAGER is None:
        _VOICE_MANAGER = VoiceLoopManager()
    return _VOICE_MANAGER
