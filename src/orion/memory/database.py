"""Persistent SQLite conversation memory and task plans for ORION."""

import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any
import uuid

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
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS task_plans (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        goal TEXT NOT NULL,
                        steps_json TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'planned',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_task_plans_user_id
                    ON task_plans(user_id)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pending_actions (
                        id TEXT PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        action_type TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        created_at REAL NOT NULL,
                        expires_at REAL NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_pending_actions_user_id
                    ON pending_actions(user_id)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS action_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        action_type TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        result_json TEXT,
                        executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_action_history_user_id
                    ON action_history(user_id)
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

    def save_task_plan(
        self,
        user_id: int,
        goal: str,
        steps: list[dict[str, Any]],
        status: str = "planned",
    ) -> int:
        """Store an action plan and return its database ID."""
        clean_goal = goal.strip()
        steps_serialized = json.dumps(steps)

        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO task_plans (user_id, goal, steps_json, status)
                    VALUES (?, ?, ?, ?)
                    """,
                    (user_id, clean_goal, steps_serialized, status),
                )
                conn.commit()
                return int(cursor.lastrowid)

    def get_recent_task_plans(self, user_id: int, limit: int = 5) -> list[dict[str, Any]]:
        """Retrieve recent plans for a user."""
        if limit <= 0:
            return []

        query = """
            SELECT id, goal, status, created_at, steps_json
            FROM task_plans
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
        """
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute(query, (user_id, limit))
                rows = cursor.fetchall()

        plans = []
        for row in rows:
            try:
                steps = json.loads(row[4])
            except Exception:
                steps = []
            plans.append(
                {
                    "id": row[0],
                    "goal": row[1],
                    "status": row[2],
                    "created_at": row[3],
                    "steps": steps,
                }
            )
        return plans

    def get_task_plan(self, user_id: int, plan_id: int) -> dict[str, Any] | None:
        """Retrieve a specific action plan by ID for a user."""
        query = """
            SELECT id, goal, steps_json, status, created_at
            FROM task_plans
            WHERE id = ? AND user_id = ?
        """
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute(query, (plan_id, user_id))
                row = cursor.fetchone()

        if not row:
            return None

        try:
            steps = json.loads(row[2])
        except Exception:
            steps = []

        return {
            "id": row[0],
            "goal": row[1],
            "steps": steps,
            "status": row[3],
            "created_at": row[4],
        }

    def create_pending_action(
        self,
        user_id: int,
        action_type: str,
        payload: dict[str, Any],
        ttl_seconds: int = 300,
    ) -> str:
        """Create a pending action requiring confirmation. Returns unguessable action ID."""
        action_id = uuid.uuid4().hex
        now = time.time()
        expires_at = now + ttl_seconds
        payload_serialized = json.dumps(payload)

        with self._lock:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO pending_actions (id, user_id, action_type, payload_json, status, created_at, expires_at)
                    VALUES (?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (action_id, user_id, action_type, payload_serialized, now, expires_at),
                )
                conn.commit()
        return action_id

    def get_pending_action(self, action_id: str, user_id: int | None = None) -> dict[str, Any] | None:
        """Retrieve a pending action. If expired and still pending, marks status as expired."""
        query = "SELECT id, user_id, action_type, payload_json, status, created_at, expires_at FROM pending_actions WHERE id = ?"
        params: list[Any] = [action_id]
        if user_id is not None:
            query += " AND user_id = ?"
            params.append(user_id)

        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute(query, params)
                row = cursor.fetchone()
                if not row:
                    return None

                act_id, uid, act_type, p_json, status, created, expires = row
                now = time.time()
                if status == "pending" and now > expires:
                    conn.execute("UPDATE pending_actions SET status = 'expired' WHERE id = ?", (act_id,))
                    conn.commit()
                    status = "expired"

        try:
            payload = json.loads(p_json)
        except Exception:
            payload = {}

        return {
            "id": act_id,
            "user_id": uid,
            "action_type": act_type,
            "payload": payload,
            "status": status,
            "created_at": created,
            "expires_at": expires,
        }

    def mark_pending_action_status(self, action_id: str, from_status: str, to_status: str) -> bool:
        """Atomically transition pending action status from from_status to to_status.

        Returns True if row was updated (transition succeeded), False otherwise.
        """
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    "UPDATE pending_actions SET status = ? WHERE id = ? AND status = ?",
                    (to_status, action_id, from_status),
                )
                updated = cursor.rowcount > 0
                conn.commit()
        return updated

    def log_action_history(
        self,
        user_id: int,
        action_type: str,
        payload: dict[str, Any],
        status: str,
        result: dict[str, Any] | str | None = None,
    ) -> int:
        """Record executed or cancelled action outcome into action_history."""
        payload_serialized = json.dumps(payload)
        result_serialized = json.dumps(result) if result is not None else None

        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO action_history (user_id, action_type, payload_json, status, result_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (user_id, action_type, payload_serialized, status, result_serialized),
                )
                conn.commit()
                return int(cursor.lastrowid)

    def get_recent_action_history(self, user_id: int, limit: int = 10) -> list[dict[str, Any]]:
        """Retrieve recent action history logs for user."""
        if limit <= 0:
            return []
        query = """
            SELECT id, action_type, payload_json, status, result_json, executed_at
            FROM action_history
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
        """
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute(query, (user_id, limit))
                rows = cursor.fetchall()

        history = []
        for row in rows:
            try:
                payload = json.loads(row[2])
            except Exception:
                payload = {}
            try:
                result = json.loads(row[4]) if row[4] else None
            except Exception:
                result = row[4]
            history.append(
                {
                    "id": row[0],
                    "action_type": row[1],
                    "payload": payload,
                    "status": row[3],
                    "result": result,
                    "executed_at": row[5],
                }
            )
        return history
