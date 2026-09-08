"""Ask threads — durable Q&A conversations for the Ask panel (SQLite, single user).

One thread = one line of inquiry (AlphaSense's "thread"); a turn = one question with its plan,
answer, and citations. Kept outside the search index so rebuilding the index never loses a
conversation, and outside the git checkout (``*.db`` is ignored, and the default location is
the estate's user area when a portable estate is configured) so it travels with the analyst's
data rather than with the code.

Schema (``SCHEMA_VERSION`` 1):
    threads(thread_id TEXT PK, title, created_at, updated_at, scope_json)
    turns(turn_id INTEGER PK, thread_id FK, ordinal, question, answer_text, mode,
          plan_json, citations_json, created_at)
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS threads (
    thread_id  TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    scope_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS turns (
    turn_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id      TEXT NOT NULL REFERENCES threads(thread_id) ON DELETE CASCADE,
    ordinal        INTEGER NOT NULL,
    question       TEXT NOT NULL,
    answer_text    TEXT,
    mode           TEXT NOT NULL,
    plan_json      TEXT NOT NULL DEFAULT '{}',
    citations_json TEXT NOT NULL DEFAULT '[]',
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS turns_thread ON turns(thread_id, ordinal);
"""


def default_threads_path() -> Path:
    """Estate user area when configured, else ``<project>/data/state/threads.db``."""
    try:
        from src.shared.paths import DATA_DIR, ESTATE_BRIDGE

        user_dir = getattr(ESTATE_BRIDGE, "uploads_dir", None)
        if user_dir is not None and Path(user_dir).parent.is_dir():
            return Path(user_dir) / "alpha_go_threads.db"
        return DATA_DIR / "state" / "threads.db"
    except Exception:  # noqa: BLE001 — bridge unavailable (tests) → project-local default
        return Path("data") / "state" / "threads.db"


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Turn:
    turn_id: int
    thread_id: str
    ordinal: int
    question: str
    answer_text: str | None
    mode: str
    plan: dict = field(default_factory=dict)
    citations: list = field(default_factory=list)
    created_at: str = ""


@dataclass
class Thread:
    thread_id: str
    title: str
    created_at: str
    updated_at: str
    scope: dict = field(default_factory=dict)
    turns: int = 0


class ThreadStore:
    """Tiny repository over the threads database (opens lazily, migrates idempotently)."""

    def __init__(self, path: "Path | str"):
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Streamlit reruns the script on a pool of threads while the store lives in
            # session_state, so the connection must not be pinned to its creating thread.
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript(_SCHEMA)
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                         (str(SCHEMA_VERSION),))
            conn.commit()
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- threads ---------------------------------------------------------------------------
    def create_thread(self, title: str, *, scope: "dict | None" = None,
                      thread_id: "str | None" = None) -> Thread:
        tid = thread_id or uuid.uuid4().hex[:12]
        now = _now()
        clean = " ".join((title or "").split())[:120] or "Untitled thread"
        self.connect().execute(
            "INSERT INTO threads(thread_id, title, created_at, updated_at, scope_json) "
            "VALUES(?,?,?,?,?)", (tid, clean, now, now, json.dumps(scope or {})))
        self.connect().commit()
        return Thread(thread_id=tid, title=clean, created_at=now, updated_at=now,
                      scope=dict(scope or {}), turns=0)

    def list_threads(self, *, limit: int = 50) -> list[Thread]:
        rows = self.connect().execute(
            "SELECT t.*, (SELECT COUNT(*) FROM turns u WHERE u.thread_id = t.thread_id) AS n "
            "FROM threads t ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [Thread(thread_id=r["thread_id"], title=r["title"], created_at=r["created_at"],
                       updated_at=r["updated_at"], scope=json.loads(r["scope_json"] or "{}"),
                       turns=int(r["n"])) for r in rows]

    def get_thread(self, thread_id: str) -> "Thread | None":
        r = self.connect().execute(
            "SELECT t.*, (SELECT COUNT(*) FROM turns u WHERE u.thread_id = t.thread_id) AS n "
            "FROM threads t WHERE thread_id = ?", (thread_id,)).fetchone()
        if r is None:
            return None
        return Thread(thread_id=r["thread_id"], title=r["title"], created_at=r["created_at"],
                      updated_at=r["updated_at"], scope=json.loads(r["scope_json"] or "{}"),
                      turns=int(r["n"]))

    def rename_thread(self, thread_id: str, title: str) -> None:
        self.connect().execute("UPDATE threads SET title = ?, updated_at = ? WHERE thread_id = ?",
                               (" ".join(title.split())[:120] or "Untitled thread", _now(), thread_id))
        self.connect().commit()

    def delete_thread(self, thread_id: str) -> None:
        self.connect().execute("DELETE FROM turns WHERE thread_id = ?", (thread_id,))
        self.connect().execute("DELETE FROM threads WHERE thread_id = ?", (thread_id,))
        self.connect().commit()

    # -- turns -----------------------------------------------------------------------------
    def add_turn(self, thread_id: str, *, question: str, answer_text: "str | None", mode: str,
                 plan: "dict | None" = None, citations: "list | None" = None) -> Turn:
        conn = self.connect()
        ordinal = conn.execute("SELECT COALESCE(MAX(ordinal), 0) + 1 AS n FROM turns "
                               "WHERE thread_id = ?", (thread_id,)).fetchone()["n"]
        now = _now()
        cur = conn.execute(
            "INSERT INTO turns(thread_id, ordinal, question, answer_text, mode, plan_json, "
            "citations_json, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (thread_id, ordinal, question, answer_text, mode, json.dumps(plan or {}),
             json.dumps(citations or []), now))
        conn.execute("UPDATE threads SET updated_at = ? WHERE thread_id = ?", (now, thread_id))
        conn.commit()
        return Turn(turn_id=int(cur.lastrowid), thread_id=thread_id, ordinal=int(ordinal),
                    question=question, answer_text=answer_text, mode=mode, plan=dict(plan or {}),
                    citations=list(citations or []), created_at=now)

    def turns(self, thread_id: str) -> list[Turn]:
        rows = self.connect().execute(
            "SELECT * FROM turns WHERE thread_id = ? ORDER BY ordinal", (thread_id,)).fetchall()
        return [Turn(turn_id=int(r["turn_id"]), thread_id=r["thread_id"], ordinal=int(r["ordinal"]),
                     question=r["question"], answer_text=r["answer_text"], mode=r["mode"],
                     plan=json.loads(r["plan_json"] or "{}"),
                     citations=json.loads(r["citations_json"] or "[]"),
                     created_at=r["created_at"]) for r in rows]

    def history(self, thread_id: str) -> list[tuple[str, str]]:
        """``(question, answer_text)`` pairs for follow-up context (answered turns only)."""
        return [(t.question, t.answer_text) for t in self.turns(thread_id) if t.answer_text]
