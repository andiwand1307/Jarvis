"""SQLite storage: conversations, API history, pending actions, log, memory notes."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    channel TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_messages (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    message TEXT NOT NULL,
    PRIMARY KEY (conversation_id, seq)
);
CREATE TABLE IF NOT EXISTS pending_actions (
    id TEXT PRIMARY KEY,
    conversation_id TEXT,
    tool TEXT NOT NULL,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    details TEXT NOT NULL,
    input TEXT NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    reported INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    conversation_id TEXT,
    tool TEXT NOT NULL,
    risk TEXT NOT NULL,
    input TEXT NOT NULL,
    result TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _short(text: str, limit: int = 300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA foreign_keys = ON")
            if str(path) != ":memory:":
                self._db.execute("PRAGMA journal_mode = WAL")
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur

    def _all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(sql, params).fetchone()

    # Conversations -----------------------------------------------------

    def create_conversation(self, title: str, channel: str = "web") -> str:
        cid = uuid.uuid4().hex
        ts = now_iso()
        title = _short(title, 60) or "Neues Gespräch"
        self._exec(
            "INSERT INTO conversations (id, title, channel, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (cid, title, channel, ts, ts),
        )
        return cid

    def get_conversation(self, cid: str) -> dict | None:
        row = self._one("SELECT * FROM conversations WHERE id = ?", (cid,))
        return dict(row) if row else None

    def list_conversations(self, limit: int = 100) -> list[dict]:
        rows = self._all(
            "SELECT id, title, updated_at FROM conversations ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def touch_conversation(self, cid: str) -> None:
        self._exec("UPDATE conversations SET updated_at = ? WHERE id = ?", (now_iso(), cid))

    def add_message(self, cid: str, role: str, text: str) -> None:
        self._exec(
            "INSERT INTO messages (conversation_id, role, text, created_at) VALUES (?, ?, ?, ?)",
            (cid, role, text, now_iso()),
        )
        self.touch_conversation(cid)

    def get_messages(self, cid: str) -> list[dict]:
        rows = self._all(
            "SELECT role, text, created_at FROM messages WHERE conversation_id = ? ORDER BY id",
            (cid,),
        )
        return [dict(r) for r in rows]

    # Full API history (append-only, needed to continue a conversation) --

    def append_api_messages(self, cid: str, messages: list[dict]) -> None:
        if not messages:
            return
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(MAX(seq), -1) FROM api_messages WHERE conversation_id = ?", (cid,)
            ).fetchone()
            seq = row[0] + 1
            for i, msg in enumerate(messages):
                self._db.execute(
                    "INSERT INTO api_messages (conversation_id, seq, message) VALUES (?, ?, ?)",
                    (cid, seq + i, json.dumps(msg, ensure_ascii=False)),
                )
            self._db.commit()

    def get_api_messages(self, cid: str) -> list[dict]:
        rows = self._all(
            "SELECT message FROM api_messages WHERE conversation_id = ? ORDER BY seq", (cid,)
        )
        return [json.loads(r["message"]) for r in rows]

    # Pending actions ---------------------------------------------------

    def create_pending(
        self,
        *,
        conversation_id: str | None,
        tool: str,
        kind: str,
        summary: str,
        details: dict,
        tool_input: dict,
    ) -> dict:
        pid = uuid.uuid4().hex[:12]
        ts = now_iso()
        self._exec(
            "INSERT INTO pending_actions (id, conversation_id, tool, kind, summary, details, input, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (
                pid,
                conversation_id,
                tool,
                kind,
                summary,
                json.dumps(details, ensure_ascii=False),
                json.dumps(tool_input, ensure_ascii=False),
                ts,
            ),
        )
        return {"id": pid, "kind": kind, "summary": summary, "details": details, "created_at": ts}

    def _pending_row(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        d["details"] = json.loads(d["details"])
        d["input"] = json.loads(d["input"])
        return d

    def get_pending(self, pid: str) -> dict | None:
        row = self._one("SELECT * FROM pending_actions WHERE id = ?", (pid,))
        return self._pending_row(row) if row else None

    def list_pending(self) -> list[dict]:
        rows = self._all(
            "SELECT id, kind, summary, details, created_at FROM pending_actions"
            " WHERE status = 'pending' ORDER BY created_at"
        )
        out = []
        for r in rows:
            d = dict(r)
            d["details"] = json.loads(d["details"])
            out.append(d)
        return out

    def claim_pending(self, pid: str, new_status: str) -> bool:
        """Atomically move a pending action out of `pending`. Only one caller wins."""
        cur = self._exec(
            "UPDATE pending_actions SET status = ?, resolved_at = ? WHERE id = ? AND status = 'pending'",
            (new_status, now_iso(), pid),
        )
        return cur.rowcount == 1

    def finish_pending(self, pid: str, status: str, result: str) -> None:
        self._exec(
            "UPDATE pending_actions SET status = ?, result = ?, resolved_at = ? WHERE id = ?",
            (status, result, now_iso(), pid),
        )

    def take_unreported(self, cid: str) -> list[dict]:
        """Resolved actions of a conversation the model has not been told about yet."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM pending_actions WHERE conversation_id = ? AND status != 'pending'"
                " AND status != 'executing' AND reported = 0 ORDER BY resolved_at",
                (cid,),
            ).fetchall()
            if rows:
                self._db.executemany(
                    "UPDATE pending_actions SET reported = 1 WHERE id = ?", [(r["id"],) for r in rows]
                )
                self._db.commit()
        return [self._pending_row(r) for r in rows]

    # Log -----------------------------------------------------------------

    def log(
        self,
        *,
        tool: str,
        risk: str,
        tool_input: Any,
        result: str,
        status: str,
        conversation_id: str | None = None,
    ) -> None:
        self._exec(
            "INSERT INTO log (created_at, conversation_id, tool, risk, input, result, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                now_iso(),
                conversation_id,
                tool,
                risk,
                json.dumps(tool_input, ensure_ascii=False),
                _short(result),
                status,
            ),
        )

    def get_log(self, limit: int = 50) -> list[dict]:
        rows = self._all("SELECT * FROM log ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["input"] = json.loads(d["input"])
            except ValueError:
                pass
            out.append(d)
        return out

    # Memory notes --------------------------------------------------------

    def add_note(self, text: str) -> int:
        cur = self._exec("INSERT INTO memory (text, created_at) VALUES (?, ?)", (text.strip(), now_iso()))
        return int(cur.lastrowid)

    def search_notes(self, query: str = "", limit: int = 50) -> list[dict]:
        if query.strip():
            terms = [t for t in query.lower().split() if t]
            rows = self._all("SELECT id, text, created_at FROM memory ORDER BY id DESC")
            hits = [dict(r) for r in rows if any(t in r["text"].lower() for t in terms)]
            return hits[:limit]
        rows = self._all("SELECT id, text, created_at FROM memory ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def delete_note(self, note_id: int) -> bool:
        cur = self._exec("DELETE FROM memory WHERE id = ?", (note_id,))
        return cur.rowcount == 1
