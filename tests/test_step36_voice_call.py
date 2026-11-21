"""Tests for Step 36 & 36.1 — Live JARVIS Voice Call HUD (Brave Local Mic & WebSocket Speech Bridge)."""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
import pytest

from orion.main import app
from orion.tools.router import route_and_execute_tool
from orion.voice.voice_call import get_voice_call_manager


@pytest.fixture(autouse=True)
def cleanup_voice_call():
    """Ensure VoiceCallManager state is clean before and after each test."""
    mgr = get_voice_call_manager()
    mgr.is_active = False
    mgr._connected_clients.clear()
    yield
    mgr.is_active = False
    mgr._connected_clients.clear()


def test_get_voice_call_hud():
    """Verify GET /voice-call returns 200 HTML with JARVIS HUD elements and Brave mic fallback APIs."""
    client = TestClient(app)
    response = client.get("/voice-call")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]

    html = response.text
    # Branding & Title
    assert "ORION // JARVIS Voice Call HUD" in html
    assert "ORION" in html

    # Core HUD Elements
    assert "orbWrapper" in html
    assert "orbStatus" in html
    assert "messagesContainer" in html
    assert "interimPreview" in html
    assert "connPill" in html
    assert "hudBanner" in html

    # Interactive Text Input Bar (Step 36.1)
    assert "textInputForm" in html
    assert "cmdInput" in html
    assert "sendBtn" in html

    # Controls
    assert "muteBtn" in html
    assert "modeBtn" in html
    assert "endCallBtn" in html

    # Client-side Browser Speech APIs
    assert "SpeechRecognition" in html
    assert "webkitSpeechRecognition" in html
    assert "speechSynthesis" in html
    assert "SpeechSynthesisUtterance" in html
    assert "/ws/voice" in html

    # Brave / Local Mic Fallback APIs (Step 36.1)
    assert "getUserMedia" in html
    assert "MediaRecorder" in html
    assert "AudioContext" in html
    assert "createAnalyser" in html
    assert "audio_b64" in html

    # Speech-Boundary Audio Chunking & VAD (Step 36.2)
    assert "SILENCE_TAIL_MS" in html
    assert "1100" in html
    assert "MIN_SPEECH_DURATION_MS" in html
    assert "600" in html
    assert "MAX_UTTERANCE_MS" in html
    assert "8500" in html
    assert "MIN_DISCARD_DURATION_MS" in html
    assert "450" in html
    assert "startRecordingChunk" in html
    assert "handleRecorderStop" in html
    assert "hasSpeech" in html
    assert "speechStartMs" in html
    # Verify fixed 3.5s interval slicing is eliminated
    assert "3500" not in html


def test_websocket_voice_direct_command():
    """Verify /ws/voice processes direct spoken commands and returns reply + speak_text."""
    client = TestClient(app)
    with client.websocket_connect("/ws/voice") as ws:
        # Send direct command without requiring wake word
        payload = {
            "text": "where is my mouse",
            "require_wake_word": False,
        }
        ws.send_text(json.dumps(payload))

        resp_raw = ws.receive_text()
        data = json.loads(resp_raw)

        assert "transcript" in data
        assert data["transcript"] == "where is my mouse"
        assert "reply" in data
        assert "Cursor Position" in data["reply"]
        assert "speak_text" in data
        # speak_text should be sanitized (no Markdown asterisks or raw emojis)
        assert "*" not in data["speak_text"]
        assert "🖱️" not in data["speak_text"]
        assert "Cursor Position" in data["speak_text"]
        assert "<" not in data["speak_text"] and ">" not in data["speak_text"]


def test_websocket_voice_audio_blob_streaming():
    """Verify /ws/voice decodes and transcribes base64 audio chunks (Brave fallback mode)."""
    fake_audio_bytes = b"RIFF....WAVEfmt ...."
    fake_audio_b64 = base64.b64encode(fake_audio_bytes).decode("utf-8")

    client = TestClient(app)
    with patch(
        "orion.voice.transcriber.VoiceTranscriber.transcribe_async",
        new=AsyncMock(return_value="where is my mouse"),
    ):
        with client.websocket_connect("/ws/voice") as ws:
            payload = {
                "audio_b64": fake_audio_b64,
                "mime_type": "audio/webm",
                "require_wake_word": False,
            }
            ws.send_text(json.dumps(payload))

            resp_raw = ws.receive_text()
            data = json.loads(resp_raw)

            assert data["transcript"] == "where is my mouse"
            assert "Cursor Position" in data["reply"]
            assert "Cursor Position" in data["speak_text"]


def test_websocket_voice_greetings_without_ollama():
    """Verify simple voice greetings respond immediately without Ollama inference."""
    client = TestClient(app)
    with client.websocket_connect("/ws/voice") as ws:
        # 1. "hello"
        ws.send_text(json.dumps({"text": "hello", "require_wake_word": False}))
        resp_hello = json.loads(ws.receive_text())
        assert resp_hello["transcript"] == "hello"
        assert "Online and ready, Yug. How can I help?" in resp_hello["reply"]
        assert "Online and ready, Yug. How can I help?" in resp_hello["speak_text"]

        # 2. "are you there"
        ws.send_text(json.dumps({"text": "are you there", "require_wake_word": False}))
        resp_there = json.loads(ws.receive_text())
        assert resp_there["transcript"] == "are you there"
        assert "Online and ready, Yug. How can I help?" in resp_there["reply"]


@pytest.mark.asyncio
async def test_router_greetings_without_ollama():
    """Verify deterministic routing for 'hey orion' and 'are you there' without Ollama."""
    res_hey = await route_and_execute_tool("hey orion")
    assert res_hey is not None
    assert res_hey["handled"] is True
    assert "Online and ready, Yug. How can I help?" in res_hey["reply"]

    res_there = await route_and_execute_tool("are you there")
    assert res_there is not None
    assert res_there["handled"] is True
    assert "Online and ready, Yug. How can I help?" in res_there["reply"]


def test_websocket_wake_word_filtering():
    """Verify wake-word mode filters phrases without wake word and executes valid ones."""
    client = TestClient(app)
    with client.websocket_connect("/ws/voice") as ws:
        # 1. Bare wake word prompts the user
        payload_bare = {
            "text": "Orion",
            "require_wake_word": True,
        }
        ws.send_text(json.dumps(payload_bare))
        resp_bare = json.loads(ws.receive_text())
        assert resp_bare["transcript"] == "Orion"
        assert "Online and ready, Yug. How can I help?" in resp_bare["reply"]

        # 2. Wake word + command executes successfully
        payload_cmd = {
            "text": "Hey Orion, where is my mouse",
            "require_wake_word": True,
        }
        ws.send_text(json.dumps(payload_cmd))
        resp_cmd = json.loads(ws.receive_text())
        assert resp_cmd["transcript"] == "where is my mouse"
        assert "Cursor Position" in resp_cmd["reply"]
        assert "Cursor Position" in resp_cmd["speak_text"]
        assert "*" not in resp_cmd["speak_text"]


def test_websocket_end_call_action():
    """Verify sending end_call terminates the session and broadcasts to manager."""
    mgr = get_voice_call_manager()
    mgr.is_active = True

    client = TestClient(app)
    with client.websocket_connect("/ws/voice") as ws:
        assert mgr.client_count == 1
        ws.send_text(json.dumps({"action": "end_call"}))
        resp = json.loads(ws.receive_text())
        assert resp.get("action") == "end_call"

    assert mgr.is_active is False
    assert mgr.client_count == 0


@pytest.mark.asyncio
async def test_router_voice_call_lifecycle_commands():
    """Verify deterministic routing for start, stop, hang up, and call status."""
    mgr = get_voice_call_manager()

    with patch.object(mgr, "_open_hud_browser"):
        # 1. start voice call
        res_start = await route_and_execute_tool("start voice call")
        assert res_start is not None
        assert res_start["handled"] is True
        assert "🎙️ *JARVIS Voice Call Started:*" in res_start["reply"]
        assert "http://127.0.0.1:8000/voice-call" in res_start["reply"]
        assert mgr.is_active is True
        assert "<" not in res_start["reply"] and ">" not in res_start["reply"]

        # 2. call orion alias
        res_call = await route_and_execute_tool("call orion")
        assert res_call is not None
        assert res_call["handled"] is True
        assert "JARVIS Voice Call Started" in res_call["reply"]

        # 3. voice status
        res_status = await route_and_execute_tool("voice status")
        assert res_status is not None
        assert res_status["handled"] is True
        assert "🎙️ *JARVIS Voice Call Status:*" in res_status["reply"]
        assert "• Call: *Active*" in res_status["reply"]
        assert "<" not in res_status["reply"] and ">" not in res_status["reply"]

        # 4. hang up
        res_hangup = await route_and_execute_tool("hang up")
        assert res_hangup is not None
        assert res_hangup["handled"] is True
        assert "📴 *JARVIS Voice Call Ended.*" in res_hangup["reply"]
        assert mgr.is_active is False
        assert "<" not in res_hangup["reply"] and ">" not in res_hangup["reply"]

        # 5. end voice call
        res_end = await route_and_execute_tool("end voice call")
        assert res_end is not None
        assert res_end["handled"] is True
        assert "📴 *JARVIS Voice Call Ended.*" in res_end["reply"]
