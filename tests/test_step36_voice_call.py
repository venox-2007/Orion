"""Tests for Step 36 — Live JARVIS Voice Call HUD (Real-Time Browser/WebSocket Voice Interface)."""

from __future__ import annotations

import json
from unittest.mock import patch
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
    """Verify GET /voice-call returns 200 HTML with JARVIS HUD elements and JS speech APIs."""
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
        assert "Yes?" in resp_bare["reply"]
        assert "Yes?" in resp_bare["speak_text"]

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
