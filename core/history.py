"""Car history: every connect, scan, code read, write and procedure, kept per VIN in SQLite."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import List, Optional

from .paths import user_dir

DB_PATH = user_dir("history.db")

CONNECT, SCAN, CODES, CLEAR, WRITE, FEATURE, SERVICE, NOTE = (
    "connect", "scan", "codes", "clear", "write", "feature", "service", "note")
KIND_LABELS = {CONNECT: "Connected", SCAN: "Module scan", CODES: "Fault codes", CLEAR: "Codes cleared",
               WRITE: "Configuration write", FEATURE: "Feature change", SERVICE: "Service procedure",
               NOTE: "Note"}


@dataclass
class Event:
    id: int
    vin: str
    ts: float
    kind: str
    summary: str
    data: dict

    @property
    def when(self) -> str:
        return time.strftime("%d %b %Y %H:%M", time.localtime(self.ts))

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)


class History:
    def __init__(self, path: Optional[str] = None):
        self.path = path or DB_PATH
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.lock = threading.Lock()
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, vin TEXT NOT NULL, ts REAL NOT NULL, kind TEXT NOT NULL,
                summary TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}')""")
            db.execute("CREATE INDEX IF NOT EXISTS ev_vin ON events(vin, ts)")

    def _db(self):
        return sqlite3.connect(self.path)

    def add(self, vin: str, kind: str, summary: str, data: Optional[dict] = None) -> int:
        with self.lock, self._db() as db:
            cur = db.execute("INSERT INTO events (vin, ts, kind, summary, data) VALUES (?,?,?,?,?)",
                             (vin or "unknown", time.time(), kind, summary, json.dumps(data or {})))
            return cur.lastrowid

    def vins(self) -> List[tuple]:
        """(vin, last seen, event count), most recent first."""
        with self.lock, self._db() as db:
            return db.execute("SELECT vin, MAX(ts), COUNT(*) FROM events GROUP BY vin ORDER BY MAX(ts) DESC").fetchall()

    def events(self, vin: str, kinds: Optional[List[str]] = None, limit: int = 500) -> List[Event]:
        q, args = "SELECT id, vin, ts, kind, summary, data FROM events WHERE vin=?", [vin]
        if kinds:
            q += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += kinds
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self.lock, self._db() as db:
            rows = db.execute(q, args).fetchall()
        return [Event(r[0], r[1], r[2], r[3], r[4], json.loads(r[5] or "{}")) for r in rows]

    def code_changes(self, vin: str) -> dict:
        """Compares the last two code reads: which codes are new, gone, and still there."""
        reads = self.events(vin, [CODES], limit=2)
        if len(reads) < 2:
            return {}
        now, before = set(reads[0].data.get("codes", [])), set(reads[1].data.get("codes", []))
        return {"new": sorted(now - before), "gone": sorted(before - now), "still": sorted(now & before),
                "since": reads[1].when}

    def delete_vin(self, vin: str):
        with self.lock, self._db() as db:
            db.execute("DELETE FROM events WHERE vin=?", (vin,))
