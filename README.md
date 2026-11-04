# ORION

ORION is a private, local-first, open-source personal autonomous AI assistant.

## Overview

ORION is designed from the ground up to operate locally, respecting user privacy and data sovereignty without reliance on paid proprietary APIs or remote hosting.

### Step 1: Project Foundation

This repository currently establishes the foundational architecture:
- Modular Python packaging separating configuration, core processing, integrations, tools, memory, and automated tests.
- High-performance, lightweight backend powered by FastAPI.
- Environment-driven configuration management with secure default settings.
- Initial health monitoring endpoint (`GET /health`).
- Automated testing suite setup.

---

## Architecture & Project Structure

```text
orion/
├── .env.example              # Example environment configuration template
├── .gitignore                 # Security & runtime exclusions
├── pyproject.toml             # Project metadata, dependencies, and build config
├── README.md                  # Project documentation
├── src/
│   └── orion/
│       ├── __init__.py        # Top-level package marker
│       ├── main.py            # FastAPI application entry point & routes
│       ├── config/            # Environment variable configuration
│       │   ├── __init__.py
│       │   └── settings.py
│       ├── core/              # Assistant core orchestration (stub)
│       │   └── __init__.py
│       ├── integrations/      # Local integrations (stub)
│       │   └── __init__.py
│       ├── tools/             # Autonomous tool execution modules (stub)
│       │   └── __init__.py
│       └── memory/            # Local memory management & retrieval (stub)
│           └── __init__.py
└── tests/
    ├── __init__.py
    └── test_health.py         # Automated test suite for health endpoint
```

---

## Getting Started

### Prerequisites

- Python 3.11+
- Virtual environment tool (such as `venv`)

### Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/venox-2007/Orion.git
   cd Orion
   ```

2. **Create and activate a virtual environment:**
   ```bash
   python -m venv .venv
   # Windows (PowerShell):
   .venv\Scripts\Activate.ps1
   # Linux/macOS:
   source .venv/bin/activate
   ```

3. **Install the package and dependencies:**
   ```bash
   pip install -e ".[dev]"
   ```

4. **Configure environment variables:**
   ```bash
   cp .env.example .env
   ```

### Running the Application

To start the local FastAPI server:
```bash
uvicorn orion.main:app --host 127.0.0.1 --port 8000 --reload
```

### Running Tests

Execute the automated test suite using `pytest`:
```bash
pytest
```

---

## Endpoints

### `GET /health`
Returns the operational health status of the service.

- **Response `200 OK`:**
  ```json
  {
    "status": "healthy"
  }
  ```

---

## Security & Privacy Guidelines

- **Zero Secret Commits:** Never hardcode credentials, API tokens, or secrets. All runtime secrets must be passed via environment variables (`.env`, which is strictly ignored by Git).
- **Local Isolation:** Local databases, session states, and conversation logs are excluded from source control.
