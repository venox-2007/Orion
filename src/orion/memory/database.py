"""Persistent SQLite conversation memory for ORION."""

from pathlib import Path
import sqlite3
import threading
from typing import Any

from orion.config.settings import get_settings


class ConversationMemory:
    """Manages persistent SQLite message storage for conversation turns."""

    def __init__(self, db_path: str | None = None) -> None:
        settings = get_settings()
        self.db_path = Path(db_path or settings.database_path)
        self._lock = threading.Lock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Create and return a SQLite connection."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(str(self.db_path), timeout=30.0)

    def _init_db(self) -> None:
        """Initialize database schema if it doesn't already exist."""
        with self._lock:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_messages_user_id
                    ON messages(user_id)
                    """
                )
                conn.commit()

    def add_message(self, user_id: int, role: str, content: str) -> None:
        """Store a single conversation message."""
        clean_content = content.strip()
        if not clean_content:
            return

        with self._lock:
            with self._get_connection() as conn:
                conn.execute(
                    "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
                    (user_id, role, clean_content),
                )
                conn.commit()

    def get_recent_messages(self, user_id: int, limit: int = 10) -> list[dict[str, str]]:
        """Retrieve recent messages in chronological order up to limit."""
        if limit <= 0:
            return []

        query = """
            SELECT role, content FROM (
                SELECT id, role, content FROM messages
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ?
            ) ORDER BY id ASC
        """
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute(query, (user_id, limit))
                rows = cursor.fetchall()

        return [{"role": row[0], "content": row[1]} for row in rows]

    def clear_history(self, user_id: int) -> int:
        """Delete all messages for a specific user ID and return count of deleted records."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
                deleted_count = cursor.rowcount
                conn.commit()
        return deleted_count
