"""Pending and decided actions, so a veto survives a restart.

The row is the veto: while it says `pending`, nothing has touched GitHub. Only a
decision written by the approval endpoint moves it forward, and only once.
"""

from __future__ import annotations

import sqlite3
import time
import uuid

PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"
EXECUTED = "executed"
FAILED = "failed"
EXPIRED = "expired"


class Store:
    def __init__(self, path: str = "actions.db") -> None:
        self.path = str(path)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS actions (
                    id         TEXT PRIMARY KEY,
                    sub        TEXT NOT NULL,
                    kind       TEXT NOT NULL,
                    repo       TEXT NOT NULL,
                    detail     TEXT,
                    status     TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    decided_at INTEGER,
                    result     TEXT
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row(row) -> dict | None:
        return dict(row) if row is not None else None

    def create(self, sub: str, kind: str, repo: str, detail: str, ttl: int) -> dict:
        now = int(time.time())
        action = {
            "id": uuid.uuid4().hex,
            "sub": sub,
            "kind": kind,
            "repo": repo,
            "detail": detail,
            "status": PENDING,
            "created_at": now,
            "expires_at": now + ttl,
            "decided_at": None,
            "result": None,
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO actions (id, sub, kind, repo, detail, status, created_at, expires_at)
                VALUES (:id, :sub, :kind, :repo, :detail, :status, :created_at, :expires_at)
                """,
                action,
            )
        return action

    def get(self, action_id: str) -> dict | None:
        with self._connect() as conn:
            return self._row(conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone())

    def pending(self, sub: str | None = None) -> list[dict]:
        query = "SELECT * FROM actions WHERE status = ?"
        args: list = [PENDING]
        if sub is not None:
            query += " AND sub = ?"
            args.append(sub)
        with self._connect() as conn:
            rows = conn.execute(query + " ORDER BY created_at DESC, rowid DESC", args).fetchall()
        return [dict(row) for row in rows]

    def recent(self, sub: str, limit: int = 8) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM actions WHERE sub = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (sub, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def decide(self, action_id: str, status: str) -> bool:
        """Move a pending action to a decision. Returns False if it was not pending."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE actions SET status = ?, decided_at = ? WHERE id = ? AND status = ?",
                (status, int(time.time()), action_id, PENDING),
            )
        return cursor.rowcount == 1

    def finish(self, action_id: str, status: str, result: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE actions SET status = ?, result = ? WHERE id = ?",
                (status, result, action_id),
            )

    def expire_due(self) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE actions SET status = ? WHERE status = ? AND expires_at < ?",
                (EXPIRED, PENDING, int(time.time())),
            )
        return cursor.rowcount
