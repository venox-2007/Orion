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

## Capabilities & Architecture Matrix (Steps 1–29)

| Domain | Capability | Architectural Implementation | Key Triggers / Tools |
| :--- | :--- | :--- | :--- |
| **Local LLM & Planning** | Autonomous Agent Core | Local Ollama (`qwen3.5:4b`), multi-step planning & confirmation workflows | `/plan <goal>`, multi-step tools |
| **Voice & Speech** | Offline Transcriber | `faster-whisper` (CPU int8), 60s voice note ingestion | Telegram voice notes |
| **Personal Productivity** | Tasks & Reminders | Persistent SQLite to-do engine & lightweight background reminder scheduler | `/tasks`, `remind me in 10m to...` |
| **Workstation & Power** | Process & Power Control | Top resource hogs inspection, guarded process termination, monitor shutoff, system sleep, web navigation | `what's eating my cpu`, `close notepad`, `turn off screen`, `sleep laptop`, `open google.com` |
| **Autonomous Watchdog** | Proactive Background Daemon | Periodic battery threshold (<20%, <10%, 100%), power plug, and thermal spike debounced alerting | `watchdog status`, `pause watchdog`, `resume watchdog` |
| **Virtual Input & Typing** | Keystrokes & Clipboard | Windows `SendInput` (Unicode), native clipboard read/write, compound key chaining, persistent active window focus | `get clipboard`, `copy <text>`, `type "hello" in notepad`, `type d and press enter` |
| **Long-Term Memory** | Fact Store & Briefings | SQLite `orion_memories` table, slug generation, Ollama prompt injection hook, daily executive briefing | `remember that <fact>`, `show memories`, `forget memory <id>`, `morning briefing` |
| **Remote Shell Bridge** | Non-blocking Terminal | Asynchronous PowerShell & CMD runner, dangerous command blocklist, 25s timeout & 3.5k output cap | `ps: <cmd>`, `cmd: <cmd>`, `run: <cmd>` |
| **Vision & Coordinates** | Mouse & UI Automation | Native mouse positioning, clicks, scrolling (`user32`), and UI Automation element auto-clicker | `click at <x>, <y>`, `scroll down <n>`, `where is my mouse`, `click on Save button` |

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

### Deterministic Command Cheatsheet

ORION executes core workstation, input, terminal, memory, and monitoring commands deterministically with zero latency, zero cloud costs, and no LLM fall-through.

#### 1. Remote Shell & Terminal
| Trigger Pattern | Action |
| :--- | :--- |
| `ps: <command>` | Run PowerShell command asynchronously (`ps: Get-Process`) |
| `cmd: <command>` | Run CMD command (`cmd: dir /b`) |
| `run: <command>` | Alias for terminal execution (`run: ipconfig`) |

#### 2. Virtual Input & Keystroke Injection
| Trigger Pattern | Action |
| :--- | :--- |
| `type <text> in <app>` | Bring target app to foreground and inject Unicode text (`type "Hello" in notepad`) |
| `type <text> and press enter` | Inject text into active window and press special key |
| `press <key>` | Tap special navigation/action key (`press enter`, `press esc`, `press tab`) |
| `get clipboard` | Read current text on the Windows clipboard (`what's on my clipboard`) |
| `copy <text>` | Set Windows clipboard contents (`copy: https://github.com`) |

#### 3. Mouse Control & UI Vision
| Trigger Pattern | Action |
| :--- | :--- |
| `click at <x>, <y>` | Move mouse cursor to screen coordinates and click (`click at 500, 400`) |
| `click <element name>` | Locate named UI control via Windows accessibility and click center (`click on Save button`, `click File`) |
| `double click [at <x>, <y>]` | Double-click active position or specified coordinates (`double click at 200, 300`) |
| `right click [at <x>, <y>]` | Right-click active position or specified coordinates |
| `scroll down <n>` / `scroll up <n>` | Scroll active window vertically by *n* wheel steps (`scroll down 5`) |
| `where is my mouse` | Inspect current cursor coordinates on screen (`get mouse pos`) |

#### 4. Workstation, Processes & Power
| Trigger Pattern | Action |
| :--- | :--- |
| `what's eating my ram` | List top memory-consuming processes (`top ram processes`) |
| `what's eating my cpu` | List top CPU-consuming processes (`top cpu processes`) |
| `close <app>` | Safely terminate user application by name or alias (`close chrome`, `kill notepad`) |
| `turn off screen` | Immediately power down display monitors (`turn off display`) |
| `sleep laptop` | Safely put computer to sleep via Windows power APIs |
| `open <url>` / `search <query>` | Open link in default browser or perform Google search (`search quantum computing`) |

#### 5. Long-Term Memory & Executive Briefings
| Trigger Pattern | Action |
| :--- | :--- |
| `morning briefing` / `daily briefing` | Generate full executive report (vitals, pending tasks, reminders, memories) |
| `remember that <fact>` | Save persistent fact with auto-slug key (`remember that server port is 8080`) |
| `show memories` / `my memories` | List all stored facts (`what do you remember about me`) |
| `forget memory <id or key>` | Delete a stored memory by numeric ID or slug key |

#### 6. Autonomous Watchdog
| Trigger Pattern | Action |
| :--- | :--- |
| `watchdog status` | Check battery, load, and background monitoring status |
| `pause watchdog` | Temporarily halt proactive background alerts |
| `resume watchdog` | Resume proactive background monitoring alerts |

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
