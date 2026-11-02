"""SQLite storage for leads and messages."""
from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    phone TEXT PRIMARY KEY,
    profile_name TEXT,
    name TEXT,
    service TEXT,
    urgency TEXT,
    preferred_time TEXT,
    score TEXT NOT NULL DEFAULT 'cold',
    status TEXT NOT NULL DEFAULT 'qualifying',
    bot_turns INTEGER NOT NULL DEFAULT 0,
    alert_sent INTEGER NOT NULL DEFAULT 0,
    crm_synced INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL,
    direction TEXT NOT NULL,
    body TEXT NOT NULL,
    wa_message_id TEXT UNIQUE,
    delivery TEXT NOT NULL DEFAULT 'ok',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL,
    body TEXT NOT NULL,
    delivered INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_phone ON messages(phone, id);
"""

LEAD_FIELDS = {
    "profile_name", "name", "service", "urgency", "preferred_time", "score",
    "status", "bot_turns", "alert_sent", "crm_synced",
}


class Store:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._db.commit()

    def add_inbound(self, phone: str, body: str, message_id: str) -> bool:
        """Store a customer message. Returns False if this message ID was already seen."""
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO messages (phone, direction, body, wa_message_id, created_at) "
                    "VALUES (?, 'in', ?, ?, ?)",
                    (phone, body, message_id, time.time()),
                )
            except sqlite3.IntegrityError:
                return False
            self._db.commit()
            return True

    def add_outbound(self, phone: str, body: str, delivered: bool = True) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO messages (phone, direction, body, delivery, created_at) "
                "VALUES (?, 'out', ?, ?, ?)",
                (phone, body, "ok" if delivered else "failed", time.time()),
            )
            self._db.commit()

    def add_alert(self, phone: str, body: str, delivered: bool) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO alerts (phone, body, delivered, created_at) VALUES (?, ?, ?, ?)",
                (phone, body, int(delivered), time.time()),
            )
            self._db.commit()

    def get_or_create_lead(self, phone: str, profile_name: str | None = None) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            self._db.execute(
                "INSERT OR IGNORE INTO leads (phone, profile_name, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (phone, profile_name, now, now),
            )
            self._db.commit()
            row = self._db.execute("SELECT * FROM leads WHERE phone = ?", (phone,)).fetchone()
            return dict(row)

    def update_lead(self, phone: str, **fields: Any) -> None:
        unknown = set(fields) - LEAD_FIELDS
        if unknown:
            raise ValueError(f"Unknown lead fields: {sorted(unknown)}")
        columns = ", ".join(f"{name} = ?" for name in fields)
        with self._lock:
            self._db.execute(
                f"UPDATE leads SET {columns}, updated_at = ? WHERE phone = ?",
                (*fields.values(), time.time(), phone),
            )
            self._db.commit()

    def history(self, phone: str, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT direction, body, delivery, created_at FROM messages "
                "WHERE phone = ? ORDER BY id DESC LIMIT ?",
                (phone, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def leads(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM leads ORDER BY updated_at DESC").fetchall()
        return [dict(row) for row in rows]

    def alerts(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT phone, body, delivered, created_at FROM alerts ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def reset(self) -> None:
        with self._lock:
            self._db.executescript("DELETE FROM leads; DELETE FROM messages; DELETE FROM alerts;")
            self._db.commit()
