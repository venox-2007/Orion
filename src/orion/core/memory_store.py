"""SQLite-backed persistent long-term memory store for ORION (Step 26)."""

from __future__ import annotations

import logging
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any

from orion.config.settings import get_settings

logger = logging.getLogger("orion.core.memory_store")

ALLOWED_CATEGORIES = {"identity", "preference", "tech", "schedule", "general"}


class MemoryStore:
    """Persistent long-term memory store backed by SQLite."""

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
        """Create orion_memories table and indexes if not exists."""
        with self._lock:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS orion_memories (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        key TEXT UNIQUE NOT NULL,
                        fact TEXT NOT NULL,
                        category TEXT DEFAULT 'general',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_memories_key ON orion_memories(key);"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_memories_category ON orion_memories(category);"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_memories_updated_at ON orion_memories(updated_at DESC, id DESC);"
                )
                conn.commit()

    @staticmethod
    def _generate_slug(fact: str) -> str:
        """Generate a slug key from the first 3-5 alphanumeric words of a fact."""
        words = re.findall(r"[a-zA-Z0-9]+", fact.lower())
        if not words:
            return f"mem_{int(time.time())}"
        # Take up to 4 words
        slug = "_".join(words[:4])
        return slug[:50]

    def save_fact(
        self,
        fact: str,
        key: str | None = None,
        category: str = "general",
    ) -> dict[str, Any]:
        """Save or update a fact in the persistent memory store."""
        clean_fact = fact.strip()
        if not clean_fact:
            raise ValueError("Fact cannot be empty.")

        if key is not None and key.strip():
            clean_key = re.sub(r"[^a-zA-Z0-9_\-]+", "_", key.strip().lower()).strip("_")
            if not clean_key:
                clean_key = self._generate_slug(clean_fact)
        else:
            clean_key = self._generate_slug(clean_fact)

        clean_category = category.strip().lower() if category else "general"
        if clean_category not in ALLOWED_CATEGORIES:
            clean_category = "general"

        with self._lock:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO orion_memories (key, fact, category, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET
                        fact = excluded.fact,
                        category = excluded.category,
                        updated_at = CURRENT_TIMESTAMP;
                    """,
                    (clean_key, clean_fact, clean_category),
                )
                conn.commit()

                cur = conn.execute(
                    """
                    SELECT id, key, fact, category, created_at, updated_at
                    FROM orion_memories
                    WHERE key = ?
                    """,
                    (clean_key,),
                )
                row = cur.fetchone()
                if row is None:
                    raise RuntimeError("Failed to retrieve saved memory record.")

                return {
                    "id": row["id"],
                    "key": row["key"],
                    "fact": row["fact"],
                    "category": row["category"],
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["updated_at"]),
                }

    def get_all_memories(self, limit: int = 50) -> list[dict[str, Any]]:
        """Retrieve all stored memories ordered by updated_at DESC."""
        with self._lock:
            with self._get_connection() as conn:
                cur = conn.execute(
                    """
                    SELECT id, key, fact, category, created_at, updated_at
                    FROM orion_memories
                    ORDER BY updated_at DESC, id DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
                rows = cur.fetchall()
                return [
                    {
                        "id": r["id"],
                        "key": r["key"],
                        "fact": r["fact"],
                        "category": r["category"],
                        "created_at": str(r["created_at"]),
                        "updated_at": str(r["updated_at"]),
                    }
                    for r in rows
                ]

    def find_memories(self, query: str) -> list[dict[str, Any]]:
        """Search memories using SQL LIKE across key and fact."""
        clean_q = query.strip()
        if not clean_q:
            return []

        pattern = f"%{clean_q}%"
        with self._lock:
            with self._get_connection() as conn:
                cur = conn.execute(
                    """
                    SELECT id, key, fact, category, created_at, updated_at
                    FROM orion_memories
                    WHERE key LIKE ? OR fact LIKE ?
                    ORDER BY updated_at DESC, id DESC
                    """,
                    (pattern, pattern),
                )
                rows = cur.fetchall()
                return [
                    {
                        "id": r["id"],
                        "key": r["key"],
                        "fact": r["fact"],
                        "category": r["category"],
                        "created_at": str(r["created_at"]),
                        "updated_at": str(r["updated_at"]),
                    }
                    for r in rows
                ]

    def delete_memory(self, identifier: str) -> bool:
        """Delete memory by numeric ID or by key slug. Return True if deleted."""
        clean_id = identifier.strip()
        if not clean_id:
            return False

        with self._lock:
            with self._get_connection() as conn:
                if clean_id.isdigit():
                    cur = conn.execute(
                        "DELETE FROM orion_memories WHERE id = ?;",
                        (int(clean_id),),
                    )
                else:
                    cur = conn.execute(
                        "DELETE FROM orion_memories WHERE key = ?;",
                        (clean_id.lower(),),
                    )
                conn.commit()
                return cur.rowcount > 0

    def format_memories_list(self, memories: list[dict[str, Any]]) -> str:
        """Format memories list for human-friendly Telegram output with clean Markdown."""
        if not memories:
            return "🧠 *No memories stored yet.* Use 'remember that [fact]' to store information."

        lines = ["🧠 *ORION Stored Memories:*"]
        for m in memories:
            lines.append(f"• [#{m['id']}] *{m['key']}*: {m['fact']}")
        return "\n".join(lines)

    def get_prompt_injection_context(self, limit: int = 15) -> str:
        """Return a concise, high-density bulleted block of known facts (max 500 characters)."""
        memories = self.get_all_memories(limit=limit)
        if not memories:
            return ""

        bullets: list[str] = []
        total_len = 0
        for m in memories:
            bullet = f"• {m['key']}: {m['fact']}"
            if total_len + len(bullet) + 1 > 500:
                break
            bullets.append(bullet)
            total_len += len(bullet) + 1

        return "\n".join(bullets)


_default_memory_store: MemoryStore | None = None


def get_memory_store(db_path: Path | str | None = None) -> MemoryStore:
    """Return a singleton or custom MemoryStore instance."""
    global _default_memory_store
    if db_path is not None:
        return MemoryStore(db_path=db_path)
    if _default_memory_store is None:
        _default_memory_store = MemoryStore()
    return _default_memory_store
