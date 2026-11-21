"""Real-time JARVIS Voice Call HUD and WebSocket speech bridge for ORION (Step 36)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import subprocess
from typing import Any
import webbrowser

from fastapi import WebSocket, WebSocketDisconnect

from orion.voice import tts
from orion.voice.stt import extract_wake_command

logger = logging.getLogger("orion.voice.voice_call")


class VoiceCallManager:
    """Manages the lifecycle of JARVIS Voice Call HUD sessions and WebSocket clients."""

    def __init__(self, hud_url: str = "http://127.0.0.1:8000/voice-call") -> None:
        self.hud_url = hud_url
        self.is_active = False
        self._connected_clients: set[WebSocket] = set()

    @property
    def client_count(self) -> int:
        """Return number of actively connected HUD WebSocket clients."""
        return len(self._connected_clients)

    def start_call(self) -> tuple[bool, str]:
        """Mark call state active and open the Voice Call HUD in Brave/default browser."""
        self.is_active = True
        self._open_hud_browser()
        return (
            True,
            f"🎙️ *JARVIS Voice Call Started:* Opened live voice HUD at `{self.hud_url}`.",
        )

    def _open_hud_browser(self) -> None:
        """Launch the HUD URL in Brave if installed, or fallback to default browser."""
        cand_paths = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "BraveSoftware"
            / "Brave-Browser"
            / "Application"
            / "brave.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
            / "BraveSoftware"
            / "Brave-Browser"
            / "Application"
            / "brave.exe",
        ]
        local_ad = os.environ.get("LOCALAPPDATA")
        if local_ad:
            cand_paths.append(
                Path(local_ad)
                / "BraveSoftware"
                / "Brave-Browser"
                / "Application"
                / "brave.exe"
            )

        launched = False
        for cp in cand_paths:
            if cp.is_file():
                try:
                    subprocess.Popen([str(cp), self.hud_url])
                    launched = True
                    break
                except Exception as e:
                    logger.debug("Failed to spawn Brave for HUD: %s", e)

        if not launched:
            try:
                webbrowser.open(self.hud_url)
            except Exception as e:
                logger.debug("Failed to open HUD URL via webbrowser: %s", e)

    def end_call(self) -> tuple[bool, str]:
        """Broadcast end_call to connected clients, mark call inactive, and stop TTS."""
        self.is_active = False
        tts.stop_speaking()

        if self._connected_clients:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._broadcast_end_call())
            except RuntimeError:
                pass

        return True, "📴 *JARVIS Voice Call Ended.*"

    async def _broadcast_end_call(self) -> None:
        """Broadcast end_call signal to all active HUD websockets."""
        clients = list(self._connected_clients)
        for ws in clients:
            try:
                await ws.send_json({"action": "end_call"})
            except Exception:
                pass

    def status(self) -> str:
        """Report live voice call status in pure Telegram Markdown."""
        state = "Active" if self.is_active else "Inactive"
        return (
            f"🎙️ *JARVIS Voice Call Status:*\n"
            f"• Call: *{state}*\n"
            f"• Connected Clients: *{self.client_count}*\n"
            f"• HUD URL: `{self.hud_url}`"
        )

    def register_client(self, ws: WebSocket) -> None:
        """Register active WebSocket client connection."""
        self._connected_clients.add(ws)

    def unregister_client(self, ws: WebSocket) -> None:
        """Unregister active WebSocket client connection."""
        self._connected_clients.discard(ws)


_VOICE_CALL_MANAGER: VoiceCallManager | None = None


def get_voice_call_manager() -> VoiceCallManager:
    """Return singleton VoiceCallManager instance."""
    global _VOICE_CALL_MANAGER
    if _VOICE_CALL_MANAGER is None:
        _VOICE_CALL_MANAGER = VoiceCallManager()
    return _VOICE_CALL_MANAGER


async def handle_voice_websocket(websocket: WebSocket) -> None:
    """Handle real-time two-way voice communication over WebSocket."""
    await websocket.accept()
    mgr = get_voice_call_manager()
    mgr.register_client(websocket)

    from orion.tools.router import route_and_execute_tool

    try:
        while True:
            raw_msg = await websocket.receive_text()
            if not raw_msg or not raw_msg.strip():
                continue

            try:
                payload = json.loads(raw_msg)
            except Exception:
                payload = {"text": raw_msg, "require_wake_word": False}

            action = payload.get("action")
            if action == "end_call":
                mgr.end_call()
                await websocket.send_json({"action": "end_call"})
                break

            spoken_text = str(payload.get("text", "")).strip()
            if not spoken_text:
                continue

            require_wake_word = bool(payload.get("require_wake_word", False))

            target_cmd: str | None = None
            if require_wake_word:
                has_wake, cmd = extract_wake_command(spoken_text, wake_word="orion")
                if not has_wake:
                    # Ignore speech that does not mention the wake word
                    continue
                if not cmd:
                    # Bare wake word was spoken
                    reply = "Yes? I am listening."
                    await websocket.send_json(
                        {
                            "transcript": spoken_text,
                            "reply": reply,
                            "speak_text": reply,
                        }
                    )
                    continue
                target_cmd = cmd
            else:
                target_cmd = spoken_text

            if not target_cmd:
                continue

            logger.info("Voice Call incoming command: '%s'", target_cmd)

            # Route command deterministically through ORION
            res = await route_and_execute_tool(
                target_cmd, context={"_voice": True, "source": "voice_call"}
            )
            if res and res.get("handled"):
                reply = res.get("reply", "Command executed.")
            else:
                reply = f"Sorry, I did not understand '{target_cmd}'."

            # Sanitize reply for speech
            speak_text = tts.sanitize_for_speech(reply)

            await websocket.send_json(
                {
                    "transcript": target_cmd,
                    "reply": reply,
                    "speak_text": speak_text,
                }
            )

    except WebSocketDisconnect:
        logger.debug("Voice HUD WebSocket client disconnected.")
    except Exception as err:
        logger.error("Voice HUD WebSocket error: %s", err)
    finally:
        mgr.unregister_client(websocket)


def get_hud_html() -> str:
    """Return the self-contained HTML/CSS/JS for the JARVIS Voice Call HUD."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ORION // JARVIS Voice Call HUD</title>
  <style>
    :root {
      --bg: #070b12;
      --card-bg: rgba(13, 20, 36, 0.75);
      --border: #1a2744;
      --cyan: #00f0ff;
      --cyan-glow: rgba(0, 240, 255, 0.35);
      --amber: #ffaa00;
      --amber-glow: rgba(255, 170, 0, 0.35);
      --green: #00ff88;
      --green-glow: rgba(0, 255, 136, 0.35);
      --red: #ff3366;
      --red-glow: rgba(255, 51, 102, 0.35);
      --text: #e2e8f0;
      --text-muted: #8295b5;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      background-color: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: space-between;
      padding: 1.5rem;
      overflow-x: hidden;
    }

    header {
      width: 100%;
      max-width: 800px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 1rem;
      border-bottom: 1px solid var(--border);
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 0.75rem;
      font-size: 1.25rem;
      font-weight: 700;
      letter-spacing: 0.15em;
      color: var(--cyan);
      text-transform: uppercase;
    }

    .status-pill {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.35rem 0.85rem;
      border-radius: 9999px;
      font-size: 0.75rem;
      font-weight: 600;
      letter-spacing: 0.05em;
      background: rgba(0, 240, 255, 0.1);
      border: 1px solid var(--cyan);
      color: var(--cyan);
    }

    .pill-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--cyan);
      box-shadow: 0 0 8px var(--cyan);
    }

    /* Central Orb HUD */
    .hud-center {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      margin: 1.5rem 0;
      gap: 1.25rem;
    }

    .orb-wrapper {
      position: relative;
      width: 170px;
      height: 170px;
      display: flex;
      align-items: center;
      justify-content: center;
    }

    .orb-ring {
      position: absolute;
      width: 100%;
      height: 100%;
      border-radius: 50%;
      border: 2px dashed var(--cyan);
      animation: rotateRing 20s linear infinite;
      opacity: 0.6;
    }

    .orb-core {
      width: 110px;
      height: 110px;
      border-radius: 50%;
      background: radial-gradient(circle, var(--cyan) 0%, rgba(0, 112, 243, 0.4) 60%, transparent 100%);
      box-shadow: 0 0 40px var(--cyan-glow);
      transition: all 0.4s ease;
      animation: pulseCore 2.5s ease-in-out infinite;
    }

    /* Orb States */
    .orb-wrapper.thinking .orb-core {
      background: radial-gradient(circle, var(--amber) 0%, rgba(255, 170, 0, 0.4) 60%, transparent 100%);
      box-shadow: 0 0 50px var(--amber-glow);
      animation: spinThinking 1s ease-in-out infinite;
    }
    .orb-wrapper.thinking .orb-ring {
      border-color: var(--amber);
      animation-duration: 4s;
    }

    .orb-wrapper.speaking .orb-core {
      background: radial-gradient(circle, var(--green) 0%, rgba(0, 255, 136, 0.4) 60%, transparent 100%);
      box-shadow: 0 0 50px var(--green-glow);
      animation: pulseSpeaking 1.2s ease-in-out infinite;
    }
    .orb-wrapper.speaking .orb-ring {
      border-color: var(--green);
      animation-duration: 8s;
    }

    .orb-wrapper.muted .orb-core {
      background: radial-gradient(circle, var(--red) 0%, rgba(255, 51, 102, 0.4) 60%, transparent 100%);
      box-shadow: 0 0 30px var(--red-glow);
      animation: none;
      transform: scale(0.9);
    }
    .orb-wrapper.muted .orb-ring {
      border-color: var(--red);
      animation: none;
    }

    .orb-status-text {
      font-size: 1.1rem;
      font-weight: 600;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: var(--cyan);
      min-height: 1.6rem;
    }

    /* Transcript Card */
    .transcript-card {
      width: 100%;
      max-width: 800px;
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 1.25rem;
      display: flex;
      flex-direction: column;
      backdrop-filter: blur(12px);
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
      flex: 1;
      min-height: 240px;
      max-height: 380px;
    }

    .transcript-header {
      font-size: 0.8rem;
      letter-spacing: 0.1em;
      color: var(--text-muted);
      text-transform: uppercase;
      margin-bottom: 0.75rem;
      padding-bottom: 0.5rem;
      border-bottom: 1px solid rgba(255, 255, 255, 0.05);
    }

    .messages-container {
      flex: 1;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
      padding-right: 0.5rem;
    }

    .message {
      padding: 0.65rem 0.95rem;
      border-radius: 10px;
      font-size: 0.92rem;
      line-height: 1.45;
      max-width: 85%;
      word-wrap: break-word;
    }

    .message.user {
      align-self: flex-end;
      background: rgba(0, 240, 255, 0.12);
      border: 1px solid rgba(0, 240, 255, 0.25);
      color: #ffffff;
    }

    .message.orion {
      align-self: flex-start;
      background: rgba(26, 39, 68, 0.6);
      border: 1px solid var(--border);
      color: var(--text);
    }

    .msg-label {
      font-weight: 700;
      font-size: 0.75rem;
      letter-spacing: 0.05em;
      margin-bottom: 0.2rem;
      display: block;
    }

    .message.user .msg-label { color: var(--cyan); }
    .message.orion .msg-label { color: #a5b4fc; }

    .interim-preview {
      font-style: italic;
      color: var(--cyan);
      opacity: 0.75;
      font-size: 0.88rem;
      min-height: 1.25rem;
      margin-top: 0.5rem;
    }

    /* Controls Bar */
    .controls-bar {
      width: 100%;
      max-width: 800px;
      display: flex;
      gap: 1rem;
      justify-content: center;
      margin-top: 1.25rem;
      flex-wrap: wrap;
    }

    .btn {
      background: rgba(13, 20, 36, 0.85);
      border: 1px solid var(--border);
      color: var(--text);
      font-size: 0.88rem;
      font-weight: 600;
      padding: 0.75rem 1.4rem;
      border-radius: 12px;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 0.5rem;
      transition: all 0.2s ease;
      user-select: none;
    }

    .btn:hover {
      background: rgba(26, 39, 68, 0.9);
      border-color: var(--cyan);
      color: var(--cyan);
    }

    .btn.active {
      background: rgba(0, 240, 255, 0.15);
      border-color: var(--cyan);
      color: var(--cyan);
      box-shadow: 0 0 15px var(--cyan-glow);
    }

    .btn-hangup {
      border-color: var(--red);
      color: var(--red);
      background: rgba(255, 51, 102, 0.1);
    }

    .btn-hangup:hover {
      background: var(--red);
      color: #ffffff;
      box-shadow: 0 0 20px var(--red-glow);
    }

    @keyframes rotateRing {
      0% { transform: rotate(0deg); }
      100% { transform: rotate(360deg); }
    }

    @keyframes pulseCore {
      0%, 100% { transform: scale(1); opacity: 0.9; }
      50% { transform: scale(1.08); opacity: 1; }
    }

    @keyframes pulseSpeaking {
      0%, 100% { transform: scale(1); }
      50% { transform: scale(1.15); }
    }

    @keyframes spinThinking {
      0% { transform: rotate(0deg) scale(0.95); }
      50% { transform: rotate(180deg) scale(1.1); }
      100% { transform: rotate(360deg) scale(0.95); }
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <span>⚡ ORION</span>
      <span style="font-size: 0.75rem; color: var(--text-muted); font-weight: 400;">// VOICE HUD</span>
    </div>
    <div class="status-pill" id="connPill">
      <div class="pill-dot" id="pillDot"></div>
      <span id="connStatus">CONNECTING</span>
    </div>
  </header>

  <main class="hud-center">
    <div class="orb-wrapper" id="orbWrapper">
      <div class="orb-ring"></div>
      <div class="orb-core"></div>
    </div>
    <div class="orb-status-text" id="orbStatus">Listening...</div>
  </main>

  <section class="transcript-card">
    <div class="transcript-header">Live Audio Transcript</div>
    <div class="messages-container" id="messagesContainer">
      <div class="message orion">
        <span class="msg-label">ORION</span>
        JARVIS Voice Call established. Speak your command or say "Orion" to begin.
      </div>
    </div>
    <div class="interim-preview" id="interimPreview"></div>
  </section>

  <footer class="controls-bar">
    <button class="btn" id="muteBtn" onclick="toggleMute()">
      <span id="muteIcon">🎙️</span>
      <span id="muteText">Mute Mic</span>
    </button>
    <button class="btn" id="modeBtn" onclick="toggleWakeWordMode()">
      <span>🔒</span>
      <span id="modeText">Mode: Always Listening</span>
    </button>
    <button class="btn btn-hangup" id="endCallBtn" onclick="hangUp()">
      <span>📴</span>
      <span>End Call</span>
    </button>
  </footer>

  <script>
    let ws = null;
    let recognition = null;
    let isMuted = false;
    let isSpeaking = false;
    let requireWakeWord = false;
    let callActive = true;

    const orbWrapper = document.getElementById('orbWrapper');
    const orbStatus = document.getElementById('orbStatus');
    const connPill = document.getElementById('connPill');
    const pillDot = document.getElementById('pillDot');
    const connStatus = document.getElementById('connStatus');
    const messagesContainer = document.getElementById('messagesContainer');
    const interimPreview = document.getElementById('interimPreview');
    const muteText = document.getElementById('muteText');
    const muteIcon = document.getElementById('muteIcon');
    const modeText = document.getElementById('modeText');

    function setOrbState(state) {
      orbWrapper.className = 'orb-wrapper ' + state;
      if (state === 'listening') {
        orbStatus.innerText = 'Listening...';
        orbStatus.style.color = 'var(--cyan)';
      } else if (state === 'thinking') {
        orbStatus.innerText = 'Thinking...';
        orbStatus.style.color = 'var(--amber)';
      } else if (state === 'speaking') {
        orbStatus.innerText = 'Speaking...';
        orbStatus.style.color = 'var(--green)';
      } else if (state === 'muted') {
        orbStatus.innerText = 'Muted';
        orbStatus.style.color = 'var(--red)';
      } else if (state === 'ended') {
        orbStatus.innerText = 'Call Ended';
        orbStatus.style.color = 'var(--text-muted)';
      }
    }

    function appendMessage(sender, text) {
      const msgDiv = document.createElement('div');
      msgDiv.className = 'message ' + (sender.toLowerCase() === 'you' ? 'user' : 'orion');

      const label = document.createElement('span');
      label.className = 'msg-label';
      label.innerText = sender;

      const body = document.createElement('span');
      body.innerText = text;

      msgDiv.appendChild(label);
      msgDiv.appendChild(body);
      messagesContainer.appendChild(msgDiv);
      messagesContainer.scrollTop = messagesContainer.scrollHeight;
    }

    function initWebSocket() {
      const loc = window.location;
      const wsProto = loc.protocol === 'https:' ? 'wss:' : 'ws:';
      const wsUrl = `${wsProto}//${loc.host}/ws/voice`;

      ws = new WebSocket(wsUrl);

      ws.onopen = () => {
        connStatus.innerText = 'ONLINE';
        pillDot.style.background = 'var(--green)';
        pillDot.style.boxShadow = '0 0 8px var(--green)';
        connPill.style.borderColor = 'var(--green)';
        connPill.style.color = 'var(--green)';
        if (!isMuted) setOrbState('listening');
      };

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data.action === 'end_call') {
            hangUp(false);
            return;
          }
          if (data.reply) {
            appendMessage('ORION', data.reply);
          }
          if (data.speak_text) {
            speakAloud(data.speak_text);
          } else {
            if (!isMuted) setOrbState('listening');
          }
        } catch (e) {
          console.error('Error handling message:', e);
          if (!isMuted) setOrbState('listening');
        }
      };

      ws.onclose = () => {
        connStatus.innerText = 'OFFLINE';
        pillDot.style.background = 'var(--red)';
        pillDot.style.boxShadow = '0 0 8px var(--red)';
        connPill.style.borderColor = 'var(--red)';
        connPill.style.color = 'var(--red)';
        if (callActive) {
          setTimeout(initWebSocket, 2000);
        }
      };

      ws.onerror = (err) => {
        console.error('WebSocket error:', err);
      };
    }

    function initSpeechRecognition() {
      const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SpeechRecognition) {
        orbStatus.innerText = 'SpeechRec Not Supported (Use Brave/Chrome)';
        orbStatus.style.color = 'var(--red)';
        return;
      }

      recognition = new SpeechRecognition();
      recognition.continuous = true;
      recognition.interimResults = true;
      recognition.lang = 'en-IN';

      recognition.onresult = (event) => {
        if (isMuted || isSpeaking) return;

        let interim = '';
        for (let i = event.resultIndex; i < event.results.length; ++i) {
          const trans = event.results[i][0].transcript;
          if (event.results[i].isFinal) {
            const finalClean = trans.trim();
            if (finalClean) {
              appendMessage('You', finalClean);
              interimPreview.innerText = '';
              setOrbState('thinking');
              sendSpeech(finalClean);
            }
          } else {
            interim += trans;
          }
        }
        interimPreview.innerText = interim;
      };

      recognition.onerror = (event) => {
        console.warn('SpeechRec error:', event.error);
        if (event.error === 'not-allowed') {
          orbStatus.innerText = 'Microphone Blocked';
          orbStatus.style.color = 'var(--red)';
        }
      };

      recognition.onend = () => {
        // Automatically restart speech recognition if call is active and not speaking/muted
        if (callActive && !isMuted && !isSpeaking) {
          try { recognition.start(); } catch (e) {}
        }
      };

      try {
        recognition.start();
      } catch (e) {
        console.debug('Recognition start issue:', e);
      }
    }

    function sendSpeech(text) {
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      ws.send(JSON.stringify({
        text: text,
        require_wake_word: requireWakeWord
      }));
    }

    function speakAloud(text) {
      if (!text || isMuted || !('speechSynthesis' in window)) {
        if (!isMuted) setOrbState('listening');
        return;
      }

      window.speechSynthesis.cancel();
      isSpeaking = true;
      setOrbState('speaking');

      // Temporarily halt mic recognition while ORION speaks to prevent audio feedback
      if (recognition) {
        try { recognition.stop(); } catch (e) {}
      }

      const utterance = new SpeechSynthesisUtterance(text);
      utterance.lang = 'en-US';

      utterance.onend = () => {
        isSpeaking = false;
        if (callActive && !isMuted) {
          setOrbState('listening');
          try { recognition.start(); } catch (e) {}
        }
      };

      utterance.onerror = () => {
        isSpeaking = false;
        if (callActive && !isMuted) {
          setOrbState('listening');
          try { recognition.start(); } catch (e) {}
        }
      };

      window.speechSynthesis.speak(utterance);
    }

    function toggleMute() {
      isMuted = !isMuted;
      if (isMuted) {
        muteText.innerText = 'Unmute Mic';
        muteIcon.innerText = '🔇';
        setOrbState('muted');
        window.speechSynthesis.cancel();
        if (recognition) {
          try { recognition.stop(); } catch (e) {}
        }
      } else {
        muteText.innerText = 'Mute Mic';
        muteIcon.innerText = '🎙️';
        setOrbState('listening');
        if (recognition) {
          try { recognition.start(); } catch (e) {}
        }
      }
    }

    function toggleWakeWordMode() {
      requireWakeWord = !requireWakeWord;
      if (requireWakeWord) {
        modeText.innerText = "Mode: Require 'Orion'";
        document.getElementById('modeBtn').classList.add('active');
      } else {
        modeText.innerText = 'Mode: Always Listening';
        document.getElementById('modeBtn').classList.remove('active');
      }
    }

    function hangUp(sendServerSignal = true) {
      callActive = false;
      if (sendServerSignal && ws && ws.readyState === WebSocket.OPEN) {
        try {
          ws.send(JSON.stringify({ action: 'end_call' }));
        } catch (e) {}
      }
      if (ws) {
        try { ws.close(); } catch (e) {}
      }
      if (recognition) {
        try { recognition.stop(); } catch (e) {}
      }
      if ('speechSynthesis' in window) {
        window.speechSynthesis.cancel();
      }
      setOrbState('ended');
      appendMessage('ORION', 'Call disconnected.');
    }

    // Initialize HUD on load
    window.addEventListener('load', () => {
      initWebSocket();
      initSpeechRecognition();
    });
  </script>
</body>
</html>
"""
