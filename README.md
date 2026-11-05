# ORION

ORION is a private, local-first, open-source personal autonomous AI assistant.

## Overview

ORION is designed from the ground up to operate locally, respecting user privacy and data sovereignty without reliance on paid proprietary APIs or remote hosting.

### Implemented Architecture

- **FastAPI Core**: Minimal, fast local backend with health monitoring (`GET /health`).
- **Local Ollama Integration**: Communicates with the local Ollama instance (`http://127.0.0.1:11434`) using the `qwen3.5:4b` model with automatic reasoning tag (`<think>`) stripping.
- **Persistent Conversation Memory**: Local SQLite storage preserving conversation context and message history across process restarts with configurable turn budgets and a `/reset` command.
- **Task Planning Core**: Structured action planning module generating persistent ordered plans (up to 8 steps).
- **Safe Read-Only Windows Tools**: Sandboxed read-only tools for system inspection (battery, CPU, RAM, disk) and safe directory listing/searching restricted strictly to pre-configured allowed folders.
- **Telegram Bot**: Long-polling interface via `python-telegram-bot` restricting access strictly to the authenticated owner in private chats.
- **Telegram Voice Commands**: Local, offline speech-to-text recognition via `faster-whisper` (CPU int8) allowing the authenticated owner to send natural voice notes that seamlessly execute through the existing command pipeline.
- **Environment-based Configuration**: Managed via Pydantic settings without committed credentials.

---

## Getting Started

### Prerequisites

- Python 3.11+
- Local Ollama running with the `qwen3.5:4b` model installed:
  ```bash
  ollama run qwen3.5:4b
  ```

### Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/venox-2007/Orion.git
   cd Orion
   ```

2. **Set up virtual environment:**
   ```bash
   python -m venv .venv
   # Windows (PowerShell):
   .venv\Scripts\Activate.ps1
   # Linux/macOS:
   source .venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -e ".[dev]"
   ```

4. **Configure your `.env` file:**
   ```bash
   cp .env.example .env
   ```
   Open `.env` and configure:
   - `ORION_TELEGRAM_BOT_TOKEN`: Your bot token obtained from [@BotFather](https://t.me/BotFather).
   - `ORION_TELEGRAM_OWNER_ID`: Your numeric Telegram user ID (safely obtained from [@userinfobot](https://t.me/userinfobot)).
   - `ORION_ALLOWED_DIRECTORIES`: Optional comma-separated absolute paths permitted for read-only listing/searching (e.g. `C:\Users\Username\Documents,C:\Users\Username\Downloads`). Defaults to empty (filesystem access disabled).
   - `ORION_VOICE_ENABLED`: Enable/disable Telegram voice note processing (`true` by default).
   - `ORION_WHISPER_MODEL`: Local Whisper model size (defaults to `base` multilingual; runs on CPU int8).

### Running ORION

Start the assistant with a single command:
```bash
python -m orion.bot
```
This launches the FastAPI service (accessible at `http://127.0.0.1:8000/health`) and initiates Telegram long-polling. No public domain, webhook, or port forwarding is required.

### Interacting with ORION in Telegram

- **Voice Notes**: Send any voice note up to 60 seconds. ORION transcribes the audio locally with `faster-whisper` and routes it through the standard command pipeline with confirmation buttons for any write actions.
- `/start`: Greeting and verification of owner authorization.
- `/reset`: Prompt to clear conversation memory (confirm by sending `/reset confirm`).
- `/plan <goal>`: Break down a goal into an ordered, structured action plan (up to 8 steps).
- `/tasks`: List recent persistent action plans.
- `/task <id>`: View the detailed steps and status of a specific action plan.
- `/apps`: View available approved Windows applications.
- `/windows`: Inspect currently open approved desktop windows.
- `/active`: Identify the active foreground application.
- `/status`: View running and pending task workflows.
- `/history`: Inspect recent execution history.
- `/cancel`: Cancel pending execution proposals.

### Running Tests

Execute the automated test suite:
```bash
pytest
```

---

## Security & Privacy Guidelines

- **Owner-only Access**: Only the configured Telegram user ID can interact with the bot in private chats. Unauthorized users are rejected.
- **Local Isolation**: All inference runs locally on your machine via Ollama.
- **Zero Secrets in Git**: Secrets (`.env`, tokens, IDs) are strictly excluded from source control.
