# ORION

ORION is a private, local-first, open-source personal autonomous AI assistant.

## Overview

ORION is designed from the ground up to operate locally, respecting user privacy and data sovereignty without reliance on paid proprietary APIs or remote hosting.

### Implemented Architecture

- **FastAPI Core**: Minimal, fast local backend with health monitoring (`GET /health`).
- **Local Ollama Integration**: Communicates with the local Ollama instance (`http://127.0.0.1:11434`) using the `qwen3.5:4b` model with automatic reasoning tag (`<think>`) stripping.
- **Persistent Conversation Memory**: Local SQLite storage preserving conversation context and message history across process restarts with configurable turn budgets and a `/reset` command.
- **Telegram Bot**: Long-polling interface via `python-telegram-bot` restricting access strictly to the authenticated owner in private chats.
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

### Running ORION

Start the assistant with a single command:
```bash
python -m orion.bot
```
This launches the FastAPI service (accessible at `http://127.0.0.1:8000/health`) and initiates Telegram long-polling. No public domain, webhook, or port forwarding is required.

### Available Commands in Telegram

- `/start`: Greeting and verification of owner authorization.
- `/reset`: Prompt to clear conversation memory (confirm by sending `/reset confirm`).
- `/plan <goal>`: Break down a goal into an ordered, structured action plan (up to 8 steps).
- `/tasks`: List recent persistent action plans.
- `/task <id>`: View the detailed steps and status of a specific action plan.

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
