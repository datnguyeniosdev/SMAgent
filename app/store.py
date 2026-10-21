"""Durable state in SQLite (stdlib). One generic record table + an append-only event log.

Every state transition is written in its own transaction before the engine moves
on, so a crash leaves the last committed state on disk for recovery.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.models import now

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    kind TEXT NOT NULL, id TEXT NOT NULL, task_id TEXT, run_id TEXT, state TEXT,
    body TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (kind, id));
CREATE INDEX IF NOT EXISTS records_task ON records(kind, task_id);
CREATE INDEX IF NOT EXISTS records_run ON records(kind, run_id);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, type TEXT NOT NULL,
    task_id TEXT, run_id TEXT, step_id TEXT, correlation_id TEXT, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_task ON events(task_id);
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY, task_id TEXT NOT NULL, created_at TEXT NOT NULL);
"""


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._db.close()

    def put(
        self,
        kind: str,
        rid: str,
        body: BaseModel | dict,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        state: str | None = None,
    ) -> None:
        data = (
            body.model_dump_json() if isinstance(body, BaseModel) else json.dumps(body, default=str)
        )
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO records(kind,id,task_id,run_id,state,body,updated_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(kind,id) DO UPDATE SET task_id=excluded.task_id, run_id=excluded.run_id, "
                "state=excluded.state, body=excluded.body, updated_at=excluded.updated_at",
                (kind, rid, task_id, run_id, state, data, now().isoformat()),
            )

    def get(self, kind: str, rid: str) -> dict | None:
        row = self._db.execute(
            "SELECT body FROM records WHERE kind=? AND id=?", (kind, rid)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def list(
        self,
        kind: str,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        states: list[str] | None = None,
    ) -> list[dict]:
        q, args = "SELECT body FROM records WHERE kind=?", [kind]
        if task_id is not None:
            q, args = q + " AND task_id=?", [*args, task_id]
        if run_id is not None:
            q, args = q + " AND run_id=?", [*args, run_id]
        if states:
            q += f" AND state IN ({','.join('?' * len(states))})"
            args += states
        return [json.loads(r[0]) for r in self._db.execute(q + " ORDER BY updated_at", args)]

    def event(
        self,
        type_: str,
        *,
        task_id=None,
        run_id=None,
        step_id=None,
        correlation_id=None,
        **data: Any,
    ) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO events(ts,type,task_id,run_id,step_id,correlation_id,body) VALUES(?,?,?,?,?,?,?)",
                (
                    now().isoformat(),
                    type_,
                    task_id,
                    run_id,
                    step_id,
                    correlation_id,
                    json.dumps(data, default=str),
                ),
            )

    def events(self, task_id: str | None = None, limit: int = 500) -> list[dict]:
        q = "SELECT seq,ts,type,task_id,run_id,step_id,correlation_id,body FROM events"
        args: list = []
        if task_id:
            q, args = q + " WHERE task_id=?", [task_id]
        rows = self._db.execute(q + " ORDER BY seq LIMIT ?", [*args, limit]).fetchall()
        keys = ("seq", "ts", "type", "task_id", "run_id", "step_id", "correlation_id")
        return [{**dict(zip(keys, r[:7], strict=True)), "data": json.loads(r[7])} for r in rows]

    def claim_idempotency(self, key: str, task_id: str) -> str | None:
        """Atomically claim a key. Returns the existing task id if already claimed."""
        with self._lock, self._db:
            row = self._db.execute("SELECT task_id FROM idempotency WHERE key=?", (key,)).fetchone()
            if row:
                return row[0]
            self._db.execute(
                "INSERT INTO idempotency(key,task_id,created_at) VALUES(?,?,?)",
                (key, task_id, now().isoformat()),
            )
        return None
