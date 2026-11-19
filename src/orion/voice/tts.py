"""Local Neural and System Text-to-Speech (TTS) for ORION (Step 34).

Supports Windows native SpeechSynthesizer / SAPI with clean Markdown and emoji
stripping, non-blocking playback, and immediate speech cancellation.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import re
import tempfile
import uuid

logger = logging.getLogger("orion.voice.tts")

_CURRENT_SPEECH_PROC: asyncio.subprocess.Process | None = None
_SPEECH_LOCK = asyncio.Lock()
_SPEECH_SYNTH_SCRIPT = Path(__file__).parent / "speech_synth.ps1"


def sanitize_for_speech(text: str) -> str:
    """Strip Markdown syntax, code blocks, URLs, and emojis for clean spoken prose."""
    if not text:
        return ""

    clean = text

    # 1. Strip fenced code blocks
    clean = re.sub(r"```[\s\S]*?```", " ", clean)

    # 2. Markdown links: [label](url) -> label
    clean = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", clean)

    # 3. Bare URLs
    clean = re.sub(r"https?://\S+", " ", clean)

    # 4. Inline code: `code` -> code
    clean = re.sub(r"`([^`]+)`", r"\1", clean)

    # 5. Bold / Italic / Strikethrough
    clean = re.sub(r"\*\*([^*]+)\*\*", r"\1", clean)
    clean = re.sub(r"\*([^*]+)\*", r"\1", clean)
    clean = re.sub(r"__([^_]+)__", r"\1", clean)
    clean = re.sub(r"_([^_]+)_", r"\1", clean)
    clean = re.sub(r"~~([^~]+)~~", r"\1", clean)

    # 6. Headers (# Header -> Header)
    clean = re.sub(r"(?m)^#+\s+", "", clean)

    # 7. Bullet points and numbered lists
    clean = re.sub(r"(?m)^[\s•\-\*]+\s*", "", clean)
    clean = re.sub(r"(?m)^\d+\.\s*", "", clean)

    # 8. Blockquotes (> quote)
    clean = re.sub(r"(?m)^>\s*", "", clean)

    # 9. Emojis and miscellaneous non-text unicode symbols
    emoji_pat = re.compile(
        r"[\U00010000-\U0010ffff"
        r"\u2600-\u27bf"
        r"\u2300-\u23ff"
        r"\u2b50-\u2b55"
        r"\u200d\ufe0f]",
        flags=re.UNICODE,
    )
    clean = emoji_pat.sub(" ", clean)

    # 10. Remaining Markdown styling characters
    clean = re.sub(r"[*`~#_]", "", clean)

    # 11. Normalize whitespace
    clean = re.sub(r"\s+", " ", clean).strip()

    return clean


def stop_speaking() -> None:
    """Immediately halt active TTS playback."""
    global _CURRENT_SPEECH_PROC
    if _CURRENT_SPEECH_PROC is not None:
        try:
            if _CURRENT_SPEECH_PROC.returncode is None:
                _CURRENT_SPEECH_PROC.terminate()
                _CURRENT_SPEECH_PROC.kill()
        except Exception as e:
            logger.debug("Error terminating speech process: %s", e)
        finally:
            _CURRENT_SPEECH_PROC = None


def is_speaking() -> bool:
    """Check if speech playback is currently active."""
    return _CURRENT_SPEECH_PROC is not None and _CURRENT_SPEECH_PROC.returncode is None


async def speak_text(text: str, rate: int = 1) -> bool:
    """Non-blocking async speech playback, cancelling overlapping speech before speaking."""
    clean_speech = sanitize_for_speech(text)
    if not clean_speech:
        return False

    temp_path: Path | None = None
    async with _SPEECH_LOCK:
        stop_speaking()

        global _CURRENT_SPEECH_PROC
        try:
            if not _SPEECH_SYNTH_SCRIPT.exists():
                logger.error("speech_synth.ps1 not found at %s", _SPEECH_SYNTH_SCRIPT)
                return False

            temp_path = Path(tempfile.gettempdir()) / f"orion_tts_{uuid.uuid4().hex[:8]}.txt"
            temp_path.write_text(clean_speech, encoding="utf-8")

            cmd = [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(_SPEECH_SYNTH_SCRIPT),
                "-TextFile",
                str(temp_path),
                "-Rate",
                str(rate),
            ]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            _CURRENT_SPEECH_PROC = proc

        except Exception as err:
            logger.error("Failed to start speech playback: %s", err)
            if temp_path and temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass
            return False

    try:
        await proc.wait()
        return proc.returncode == 0
    except asyncio.CancelledError:
        stop_speaking()
        raise
    finally:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass
        if _CURRENT_SPEECH_PROC is proc:
            _CURRENT_SPEECH_PROC = None
