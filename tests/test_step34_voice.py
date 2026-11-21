"""Tests for Step 34 — Local Two-Way Voice Interface (STT + Neural TTS + Voice Mode Toggle)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from orion.tools.router import route_and_execute_tool
from orion.voice import stt, tts
from orion.voice.stt import VoiceLoopManager, extract_wake_command, get_voice_loop_manager
from orion.voice.voice_call import get_voice_call_manager
from orion.voice.tts import is_speaking, sanitize_for_speech, speak_text, stop_speaking


@pytest.fixture(autouse=True)
def cleanup_voice():
    """Ensure any speech or voice loops are stopped before and after tests."""
    stop_speaking()
    mgr = get_voice_loop_manager()
    mgr._running = False
    if mgr._task and not mgr._task.done():
        mgr._task.cancel()
    yield
    stop_speaking()
    mgr = get_voice_loop_manager()
    mgr._running = False
    if mgr._task and not mgr._task.done():
        mgr._task.cancel()


def test_sanitize_for_speech():
    """Verify Markdown syntax, code blocks, URLs, and emojis are stripped cleanly."""
    raw_text = (
        "⚡ *Workflow Completed (3/3 steps):*\n"
        "1. ✅ *ps: echo 1*\n"
        "2. 👁️ Detected *'Ready'* on screen at (100, 100).\n"
        "```python\nprint('Secret Code')\n```\n"
        "Visit [GitHub](https://github.com/venox-2007/Orion) or https://google.com for info.\n"
        "> Quote block with `inline code` and ~~strikethrough~~."
    )

    clean = sanitize_for_speech(raw_text)

    # Markdown / formatting characters stripped
    assert "*" not in clean
    assert "`" not in clean
    assert "#" not in clean
    assert "~~" not in clean
    assert "```" not in clean
    assert "print('Secret Code')" not in clean  # code block removed

    # URLs removed, link label kept
    assert "https://" not in clean
    assert "GitHub" in clean

    # Emojis stripped
    assert "⚡" not in clean
    assert "✅" not in clean
    assert "👁️" not in clean

    # Prose content retained
    assert "Workflow Completed (3/3 steps):" in clean
    assert "ps: echo 1" in clean
    assert "Detected 'Ready' on screen at (100, 100)." in clean
    assert "inline code" in clean


@pytest.mark.asyncio
async def test_speak_text_and_stop_speaking():
    """Verify speak_text starts process and stop_speaking halts it."""
    wait_event = asyncio.Event()

    async def fake_wait():
        await wait_event.wait()
        return 0

    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_proc.wait = AsyncMock(side_effect=fake_wait)

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec, \
         patch("pathlib.Path.exists", return_value=True):

        # Test speak_text
        task = asyncio.create_task(speak_text("Hello testing speech"))
        await asyncio.sleep(0.01)

        mock_exec.assert_called_once()
        assert tts._CURRENT_SPEECH_PROC is mock_proc
        assert is_speaking() is True

        # Test stop_speaking terminates process
        stop_speaking()
        wait_event.set()
        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()
        assert tts._CURRENT_SPEECH_PROC is None
        assert is_speaking() is False

        await task


@pytest.mark.asyncio
async def test_listen_once_wake_word_and_confidence():
    """Verify listen_once captures recognized speech and logs hypothesis/errors."""
    # 1. Success case: confidence >= 0.35 returned
    mock_proc_ok = MagicMock()
    mock_proc_ok.communicate = AsyncMock(
        return_value=(b"Orion open notepad\n", b"Raw hypothesis: 'Orion open notepad' (confidence: 0.85)\n")
    )
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc_ok), \
         patch("pathlib.Path.exists", return_value=True):
        res = await stt.listen_once(timeout_sec=2.0)
        assert res == "Orion open notepad"

    # 2. Low confidence rejection (< 0.35): stdout empty, rejection logged to stderr
    mock_proc_low = MagicMock()
    mock_proc_low.communicate = AsyncMock(
        return_value=(b"", b"Rejected hypothesis 'Ryan': confidence 0.22 < 0.35\n")
    )
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc_low), \
         patch("pathlib.Path.exists", return_value=True):
        res = await stt.listen_once(timeout_sec=2.0)
        assert res is None

    # 3. Audio/Microphone warning in stderr
    mock_proc_err = MagicMock()
    mock_proc_err.communicate = AsyncMock(
        return_value=(b"", b"Microphone unavailable or audio input error\n")
    )
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc_err), \
         patch("pathlib.Path.exists", return_value=True):
        res = await stt.listen_once(timeout_sec=2.0)
        assert res is None


def test_extract_wake_command():
    """Verify wake word identification and subsequent command extraction."""
    # Direct command with wake word
    has_wake, cmd = extract_wake_command("Orion, put the laptop to sleep")
    assert has_wake is True
    assert cmd == "put the laptop to sleep"

    has_wake, cmd = extract_wake_command("Hey Orion open chrome")
    assert has_wake is True
    assert cmd == "open chrome"

    has_wake, cmd = extract_wake_command("Ok Orion, read screen")
    assert has_wake is True
    assert cmd == "read screen"

    # Bare wake word (prompts user)
    has_wake, cmd = extract_wake_command("Orion")
    assert has_wake is True
    assert cmd is None

    has_wake, cmd = extract_wake_command("Hey Orion!")
    assert has_wake is True
    assert cmd is None

    # No wake word present
    has_wake, cmd = extract_wake_command("hello world what is the time")
    assert has_wake is False
    assert cmd is None


@pytest.mark.asyncio
async def test_voice_loop_manager_direct_command():
    """Verify VoiceLoopManager captures speech, routes command, and speaks response."""
    spoken_replies = []

    async def mock_listen(timeout_sec: float = 4.0):
        if not hasattr(mock_listen, "called"):
            mock_listen.called = True
            return "Orion, where is my mouse"
        await asyncio.sleep(0.1)
        return None

    async def mock_speak(text: str, rate: int = 1):
        spoken_replies.append(text)
        return True

    async def mock_router(clean_text: str, context: dict | None = None):
        return {"handled": True, "reply": "📍 Mouse position is (500, 400)."}

    manager = VoiceLoopManager(
        wake_word="orion",
        listen_fn=mock_listen,
        speak_fn=mock_speak,
        router_fn=mock_router,
    )

    await manager.start()
    assert manager.is_running is True

    # Allow loop to execute 1 iteration
    await asyncio.sleep(0.15)
    await manager.stop()

    assert manager.is_running is False
    assert len(spoken_replies) == 1
    assert "📍 Mouse position is (500, 400)." in spoken_replies[0]


@pytest.mark.asyncio
async def test_voice_loop_manager_bare_wake_word_followup():
    """Verify bare wake word triggers 'Yes?' prompt and executes follow-up command."""
    spoken_replies = []
    listen_queue = ["Orion", "open notepad"]

    async def mock_listen(timeout_sec: float = 4.0):
        if listen_queue:
            return listen_queue.pop(0)
        await asyncio.sleep(0.1)
        return None

    async def mock_speak(text: str, rate: int = 1):
        spoken_replies.append(text)
        return True

    async def mock_router(clean_text: str, context: dict | None = None):
        return {"handled": True, "reply": f"Launch requested for {clean_text}."}

    manager = VoiceLoopManager(
        wake_word="orion",
        listen_fn=mock_listen,
        speak_fn=mock_speak,
        router_fn=mock_router,
    )

    await manager.start()
    await asyncio.sleep(0.2)
    await manager.stop()

    assert "Yes?" in spoken_replies
    assert any("Launch requested" in r for r in spoken_replies)


@pytest.mark.asyncio
async def test_router_voice_commands():
    """Verify deterministic routing of say, start voice, stop voice, and voice status."""
    with patch("orion.voice.tts.speak_text", new=AsyncMock(return_value=True)) as mock_speak, \
         patch("orion.voice.voice_call.VoiceCallManager._open_hud_browser"):
        # 1. Say / Speak
        res_say = await route_and_execute_tool('say "Hello Yug"')
        assert res_say is not None
        assert res_say["handled"] is True
        assert '🗣️ Speaking: *"Hello Yug"*' in res_say["reply"]
        mock_speak.assert_called_once_with("Hello Yug")

        # 2. Start Voice Mode
        res_start = await route_and_execute_tool("start voice mode")
        assert res_start is not None
        assert res_start["handled"] is True
        assert "JARVIS Voice Call Started" in res_start["reply"]
        assert get_voice_call_manager().is_active is True

        # 3. Voice Status
        res_status = await route_and_execute_tool("voice status")
        assert res_status is not None
        assert res_status["handled"] is True
        assert "JARVIS Voice Call Status" in res_status["reply"]
        assert "Call: *Active*" in res_status["reply"]

        # 4. Stop Voice Mode
        res_stop = await route_and_execute_tool("stop voice mode")
        assert res_stop is not None
        assert res_stop["handled"] is True
        assert "JARVIS Voice Call Ended" in res_stop["reply"]
        assert get_voice_call_manager().is_active is False

        # 5. Stop Speaking / Mute Voice
        with patch("orion.voice.tts.stop_speaking") as mock_stop_speak:
            res_mute = await route_and_execute_tool("stop speaking")
            assert res_mute is not None
            assert res_mute["handled"] is True
            assert "🔇 *Voice Speech Halted.*" in res_mute["reply"]
            mock_stop_speak.assert_called_once()
