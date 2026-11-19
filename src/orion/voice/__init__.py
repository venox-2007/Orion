"""Voice recognition, speech-to-text, and text-to-speech package for ORION."""

from orion.voice.stt import VoiceLoopManager, get_voice_loop_manager, listen_once
from orion.voice.transcriber import VoiceTranscriber
from orion.voice.tts import is_speaking, sanitize_for_speech, speak_text, stop_speaking

__all__ = [
    "VoiceLoopManager",
    "VoiceTranscriber",
    "get_voice_loop_manager",
    "is_speaking",
    "listen_once",
    "sanitize_for_speech",
    "speak_text",
    "stop_speaking",
]
