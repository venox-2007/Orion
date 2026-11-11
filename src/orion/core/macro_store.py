"""SQLite-backed persistent macro store for ORION (Step 30)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any

from orion.config.settings import get_settings

logger = logging.getLogger("orion.core.macro_store")


class MacroStore:
    """Persistent storage for reusable multi-step macros backed by SQLite."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        if db_path is not None:
            self.db_path = Path(db_path)
        else:
            settings = get_settings()
            self.db_path = Path(settings.db_path) if getattr(settings, "db_path", None) else Path("data/orion.db")

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Create orion_macros table and indexes if not exists."""
        with self._lock:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS orion_macros (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT UNIQUE NOT NULL,
                        description TEXT DEFAULT '',
                        steps TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_macros_name ON orion_macros(name);"
                )

    def _normalize_name(self, name: str) -> str:
        """Normalize macro name to lowercase alphanumeric slug with underscores/hyphens."""
        clean = name.strip().lower()
        clean = re.sub(r"[^a-z0-9_\-]", "_", clean)
        return clean.strip("_")

    def save_macro(self, name: str, steps: list[str], description: str = "") -> dict[str, Any]:
        """Save or overwrite a macro by name (upsert on conflict)."""
        norm_name = self._normalize_name(name)
        if not norm_name:
            raise ValueError("Macro name cannot be empty or invalid.")

        clean_steps = [s.strip() for s in steps if s and s.strip()]
        if not clean_steps:
            raise ValueError("Macro must contain at least one step.")

        steps_json = json.dumps(clean_steps)
        with self._lock:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO orion_macros (name, description, steps, created_at, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT(name) DO UPDATE SET
                        description = excluded.description,
                        steps = excluded.steps,
                        updated_at = CURRENT_TIMESTAMP;
                    """,
                    (norm_name, description.strip(), steps_json),
                )
        return self.get_macro(norm_name) or {
            "name": norm_name,
            "description": description.strip(),
            "steps": clean_steps,
        }

    def get_macro(self, name: str) -> dict[str, Any] | None:
        """Retrieve a macro by name with parsed steps list."""
        norm_name = self._normalize_name(name)
        if not norm_name:
            return None

        with self._lock:
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT id, name, description, steps, created_at, updated_at FROM orion_macros WHERE name = ?",
                    (norm_name,),
                ).fetchone()

        if not row:
            return None

        try:
            steps_list = json.loads(row["steps"])
        except Exception:
            steps_list = []

        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"] or "",
            "steps": steps_list,
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def list_macros(self) -> list[dict[str, Any]]:
        """Return all saved macros ordered by name ascending."""
        with self._lock:
            with self._get_connection() as conn:
                rows = conn.execute(
                    "SELECT id, name, description, steps, created_at, updated_at FROM orion_macros ORDER BY name ASC"
                ).fetchall()

        results = []
        for row in rows:
            try:
                steps_list = json.loads(row["steps"])
            except Exception:
                steps_list = []

            results.append({
                "id": row["id"],
                "name": row["name"],
                "description": row["description"] or "",
                "steps": steps_list,
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            })
        return results

    def delete_macro(self, name: str) -> bool:
        """Delete a macro by name. Returns True if a record was deleted."""
        norm_name = self._normalize_name(name)
        if not norm_name:
            return False

        with self._lock:
            with self._get_connection() as conn:
                cur = conn.execute("DELETE FROM orion_macros WHERE name = ?", (norm_name,))
                return cur.rowcount > 0

    def format_macro_list(self, macros: list[dict[str, Any]]) -> str:
        """Return clean Telegram Markdown listing of macros."""
        if not macros:
            return "⚡ *No macros saved yet.* Use 'macro save <name>: <cmd1> then <cmd2>' to create one."

        lines = ["⚡ *Saved ORION Macros:*"]
        for m in macros:
            steps = m.get("steps", [])
            steps_preview = " → ".join(steps)
            step_count = len(steps)
            step_label = "step" if step_count == 1 else "steps"
            lines.append(f"• *{m['name']}* ({step_count} {step_label}): {steps_preview}")
        return "\n".join(lines)


_default_macro_store: MacroStore | None = None


def get_macro_store(db_path: Path | str | None = None) -> MacroStore:
    """Return a singleton or custom MacroStore instance."""
    global _default_macro_store
    if db_path is not None:
        return MacroStore(db_path=db_path)
    if _default_macro_store is None:
        _default_macro_store = MacroStore()
    return _default_macro_store
