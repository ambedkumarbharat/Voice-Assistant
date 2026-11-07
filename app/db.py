"""SQLite persistence (WAL) for calls, messages, events, contacts and settings."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS calls (
  id TEXT PRIMARY KEY,
  direction TEXT NOT NULL DEFAULT 'web',
  transport TEXT NOT NULL DEFAULT 'browser',
  status TEXT NOT NULL DEFAULT 'in_progress',
  from_number TEXT,
  to_number TEXT,
  contact_name TEXT,
  provider_call_id TEXT,
  started_at INTEGER NOT NULL,
  answered_at INTEGER,
  ended_at INTEGER,
  duration_ms INTEGER,
  end_reason TEXT,
  turns INTEGER NOT NULL DEFAULT 0,
  summary TEXT,
  outcome TEXT,
  sentiment TEXT,
  action_items TEXT,
  metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_started ON calls(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_calls_status ON calls(status);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  call_id TEXT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  at INTEGER NOT NULL,
  latency_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_messages_call ON messages(call_id, id);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  call_id TEXT,
  type TEXT NOT NULL,
  payload TEXT,
  at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_call ON events(call_id, id);

CREATE TABLE IF NOT EXISTS contacts (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  phone TEXT NOT NULL UNIQUE,
  email TEXT,
  company TEXT,
  notes TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contacts_name ON contacts(name);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
"""

CALL_FIELDS = {
    "direction", "transport", "status", "from_number", "to_number", "contact_name",
    "provider_call_id", "started_at", "answered_at", "ended_at", "duration_ms",
    "end_reason", "turns", "summary", "outcome", "sentiment", "action_items", "metadata",
}


def now_ms() -> int:
    return int(time.time() * 1000)


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True
        with self._lock:
            try:
                self._conn.executescript("PRAGMA wal_checkpoint(TRUNCATE);")
            except (sqlite3.OperationalError, sqlite3.ProgrammingError):
                pass
            try:
                self._conn.close()
            except sqlite3.ProgrammingError:
                pass

    def ping(self) -> None:
        with self._lock:
            self._conn.execute("SELECT 1").fetchone()

    # ------------------------------------------------------------- calls ---

    def create_call(
        self,
        *,
        call_id: str | None = None,
        direction: str = "web",
        transport: str = "browser",
        status: str = "in_progress",
        from_number: str | None = None,
        to_number: str | None = None,
        contact_name: str | None = None,
        provider_call_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        call_id = call_id or str(uuid.uuid4())
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO calls (id, direction, transport, status, from_number, to_number,
                   contact_name, provider_call_id, started_at, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    call_id, direction, transport, status, from_number, to_number,
                    contact_name, provider_call_id, now_ms(),
                    json.dumps(metadata, separators=(",", ":")) if metadata is not None else None,
                ),
            )
        return self.get_call(call_id)  # type: ignore[return-value]

    def get_call(self, call_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
        return dict(row) if row else None

    def find_call_by_provider_id(self, provider_call_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM calls WHERE provider_call_id = ? ORDER BY started_at DESC LIMIT 1",
                (provider_call_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_call(self, call_id: str, patch: dict[str, Any]) -> None:
        fields = {key: value for key, value in patch.items() if key in CALL_FIELDS}
        if not fields:
            return
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE calls SET {assignments} WHERE id = ?",
                (*fields.values(), call_id),
            )

    def list_calls(
        self,
        *,
        limit: int = 25,
        offset: int = 0,
        status: str | None = None,
        query: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        limit = max(1, min(200, limit))
        offset = max(0, offset)
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if query:
            clauses.append(
                "(id LIKE ? OR COALESCE(to_number, '') LIKE ? OR COALESCE(from_number, '') LIKE ? "
                "OR COALESCE(contact_name, '') LIKE ? OR COALESCE(summary, '') LIKE ?)"
            )
            like = f"%{query}%"
            params.extend([like] * 5)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM calls {where} ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            total = self._conn.execute(
                f"SELECT COUNT(*) AS c FROM calls {where}", tuple(params)
            ).fetchone()["c"]
        return [dict(row) for row in rows], int(total)

    def count_active_calls(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM calls WHERE status IN ('in_progress','ringing','queued')"
            ).fetchone()
        return int(row["c"])

    def mark_stale_calls(self) -> int:
        current = now_ms()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """UPDATE calls SET status = 'failed', end_reason = 'process_restart',
                   ended_at = ?, duration_ms = ? - started_at
                   WHERE status IN ('queued','ringing','in_progress')""",
                (current, current),
            )
        return int(cursor.rowcount or 0)

    # ---------------------------------------------------------- messages ---

    def add_message(self, call_id: str, role: str, content: str, latency_ms: int | None = None) -> dict[str, Any]:
        at = now_ms()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO messages (call_id, role, content, at, latency_ms) VALUES (?, ?, ?, ?, ?)",
                (call_id, role, content, at, latency_ms),
            )
        return {
            "id": cursor.lastrowid,
            "call_id": call_id,
            "role": role,
            "content": content,
            "at": at,
            "latency_ms": latency_ms,
        }

    def list_messages(self, call_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE call_id = ? ORDER BY id ASC", (call_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------ events ---

    def add_event(self, call_id: str | None, event_type: str, payload: dict[str, Any] | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO events (call_id, type, payload, at) VALUES (?, ?, ?, ?)",
                (call_id, event_type, json.dumps(payload, separators=(",", ":")) if payload is not None else None, now_ms()),
            )

    def list_events(self, call_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE call_id = ? ORDER BY id DESC LIMIT ?", (call_id, limit)
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------------------------------------------------------- contacts ---

    def upsert_contact(
        self,
        *,
        name: str,
        phone: str,
        email: str | None = None,
        company: str | None = None,
        notes: str | None = None,
        contact_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            existing = self._conn.execute("SELECT * FROM contacts WHERE phone = ?", (phone,)).fetchone()
        if existing:
            with self._lock, self._conn:
                self._conn.execute(
                    "UPDATE contacts SET name = ?, email = ?, company = ?, notes = ? WHERE id = ?",
                    (name, email, company, notes, existing["id"]),
                )
            with self._lock:
                row = self._conn.execute("SELECT * FROM contacts WHERE id = ?", (existing["id"],)).fetchone()
            return dict(row)
        contact_id = contact_id or str(uuid.uuid4())
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO contacts (id, name, phone, email, company, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (contact_id, name, phone, email, company, notes, now_ms()),
            )
        with self._lock:
            row = self._conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        return dict(row)

    def list_contacts(self, query: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if query:
                like = f"%{query}%"
                rows = self._conn.execute(
                    "SELECT * FROM contacts WHERE name LIKE ? OR phone LIKE ? OR COALESCE(company,'') LIKE ? "
                    "ORDER BY created_at DESC LIMIT 200",
                    (like, like, like),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM contacts ORDER BY created_at DESC LIMIT 200"
                ).fetchall()
        return [dict(row) for row in rows]

    def delete_contact(self, contact_id: str) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
        return bool(cursor.rowcount)

    # ---------------------------------------------------------- settings ---

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
                (key, json.dumps(value, separators=(",", ":")), now_ms()),
            )

    def all_settings(self) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        result: dict[str, Any] = {}
        for row in rows:
            try:
                result[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                result[row["key"]] = row["value"]
        return result

    # --------------------------------------------------------- analytics ---

    def stats(self) -> dict[str, Any]:
        today = datetime.combine(date.today(), datetime.min.time())
        start = int(today.timestamp() * 1000)
        with self._lock:
            def scalar(sql: str, params: tuple[Any, ...] = ()) -> Any:
                row = self._conn.execute(sql, params).fetchone()
                return row[0] if row else 0

            total = scalar("SELECT COUNT(*) FROM calls")
            calls_today = scalar("SELECT COUNT(*) FROM calls WHERE started_at >= ?", (start,))
            completed = scalar("SELECT COUNT(*) FROM calls WHERE status = 'completed'")
            failed = scalar("SELECT COUNT(*) FROM calls WHERE status IN ('failed','no_answer')")
            avg = scalar("SELECT COALESCE(AVG(duration_ms), 0) FROM calls WHERE duration_ms IS NOT NULL")
            talk = scalar("SELECT COALESCE(SUM(duration_ms), 0) FROM calls")
            resolved = scalar("SELECT COUNT(*) FROM calls WHERE outcome = 'resolved'")
            messages = scalar("SELECT COUNT(*) FROM messages")
            active = scalar("SELECT COUNT(*) FROM calls WHERE status IN ('in_progress','ringing','queued')")
        return {
            "totalCalls": int(total),
            "callsToday": int(calls_today),
            "completed": int(completed),
            "failed": int(failed),
            "avgDurationMs": int(avg),
            "talkMinutes": round(int(talk) / 6000) / 10,
            "resolutionRate": round(resolved / completed * 100) if completed else 0,
            "activeCalls": int(active),
            "messages": int(messages),
        }

    def daily_volume(self, days: int = 14) -> list[dict[str, Any]]:
        since = now_ms() - days * 86_400_000
        with self._lock:
            rows = self._conn.execute(
                """SELECT strftime('%Y-%m-%d', started_at / 1000, 'unixepoch', 'localtime') AS day,
                          COUNT(*) AS calls, COALESCE(SUM(duration_ms), 0) AS total_ms
                   FROM calls WHERE started_at >= ? GROUP BY day ORDER BY day ASC""",
                (since,),
            ).fetchall()
        by_day = {row["day"]: row for row in rows}
        series: list[dict[str, Any]] = []
        for i in range(days - 1, -1, -1):
            day = date.today() - timedelta(days=i)
            key = day.isoformat()
            row = by_day.get(key)
            series.append(
                {
                    "date": key,
                    "calls": int(row["calls"]) if row else 0,
                    "minutes": round(int(row["total_ms"]) / 6000) / 10 if row else 0,
                }
            )
        return series
