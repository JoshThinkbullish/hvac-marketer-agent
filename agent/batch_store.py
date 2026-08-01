"""Durable metadata and file layout for creative batches.

SQLite stores JSON snapshots so the in-process job representation can evolve
without a migration for every UI field.  Images and uploads live beside the
database and are referenced by absolute paths in those snapshots.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class BatchStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "batches.sqlite3"
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_batches_updated
                    ON batches(updated_at DESC);

                CREATE TABLE IF NOT EXISTS revisions (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL REFERENCES batches(id)
                        ON DELETE CASCADE,
                    idempotency_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(batch_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_revisions_batch
                    ON revisions(batch_id, created_at);
                """
            )

    def batch_dir(self, batch_id: str) -> Path:
        path = self.root / "batches" / batch_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def revision_dir(self, batch_id: str, revision_id: str) -> Path:
        path = self.batch_dir(batch_id) / "revisions" / revision_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_batch(self, batch_id: str, status: str, created_at: str,
                   payload: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO batches(id, status, created_at, updated_at, payload_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    payload_json=excluded.payload_json
                """,
                (batch_id, status, created_at, now, encoded),
            )

    def load_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM batches WHERE id=?", (batch_id,)
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def list_batches(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, status, created_at, updated_at, payload_json
                FROM batches ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            result.append({
                "id": row["id"],
                "status": row["status"],
                "created": row["created_at"],
                "updated": row["updated_at"],
                "client_name": payload.get("client_name", ""),
                "item_count": len(payload.get("items", [])),
                "revision_count": len(payload.get("revision_history", [])),
            })
        return result

    def create_revision(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Insert a revision, returning (payload, created).

        A concurrent retry with the same batch/key returns the original row.
        """
        now = datetime.now(timezone.utc).isoformat()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO revisions(
                        id, batch_id, idempotency_key, status,
                        created_at, updated_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload["revision_id"], payload["batch_id"],
                        payload["idempotency_key"], payload["status"],
                        payload.get("created_at", now), now, encoded,
                    ),
                )
                return payload, True
            except sqlite3.IntegrityError:
                row = conn.execute(
                    """
                    SELECT payload_json FROM revisions
                    WHERE batch_id=? AND idempotency_key=?
                    """,
                    (payload["batch_id"], payload["idempotency_key"]),
                ).fetchone()
                if not row:
                    raise
                return json.loads(row["payload_json"]), False

    def save_revision(self, payload: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE revisions SET status=?, updated_at=?, payload_json=?
                WHERE id=?
                """,
                (payload["status"], now, encoded, payload["revision_id"]),
            )

    def load_revision(self, revision_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM revisions WHERE id=?", (revision_id,)
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def prune(self, retention_days: int) -> int:
        """Remove expired database rows. Files are left for recoverability."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max(7, retention_days))
        ).isoformat()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM batches WHERE updated_at < ?", (cutoff,)
            ).fetchall()
            conn.execute("DELETE FROM batches WHERE updated_at < ?", (cutoff,))
        return len(rows)


def default_data_root(base_dir: Path) -> Path:
    configured = os.environ.get("HVAC_DATA_DIR", "").strip()
    return Path(configured).expanduser() if configured else base_dir / "data"
