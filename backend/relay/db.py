"""SQLite storage for dashboard metadata.

Live tmux inventory is the source of truth for whether a session exists.
This database stores what tmux cannot: stable dashboard IDs, display names,
launch metadata, agent event state, activity, previews, and check runs.

One shared connection is used from the event loop and from worker threads,
guarded by a lock. Every query is small, so the lock is never held for long.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id                   TEXT PRIMARY KEY,         -- stable dashboard id (uuid4 hex)
    env                  TEXT NOT NULL,            -- environment id, e.g. 'work2'
    kind                 TEXT NOT NULL,            -- 'claude' | 'codex' | 'shell' | 'other'
    display_name         TEXT NOT NULL,
    tmux_name            TEXT NOT NULL,            -- tmux session name when last seen
    origin               TEXT NOT NULL,            -- 'dashboard' | 'discovered'
    status               TEXT NOT NULL,            -- 'live' | 'ended' | 'interrupted'
    state                TEXT NOT NULL DEFAULT 'unknown',
                                                   -- 'working' | 'needs_input' | 'idle' | 'exited' | 'unknown'
    state_source         TEXT,                     -- 'hook' | 'notify' | 'terminal' | NULL
    state_at             REAL,
    container_id         TEXT,
    container_started_at TEXT,
    socket_path          TEXT NOT NULL,            -- tmux socket path inside the container
    server_pid           INTEGER,
    tmux_session_id      TEXT,                     -- e.g. '$3'
    tmux_created         INTEGER,                  -- tmux session_created (epoch seconds)
    pane_id              TEXT,                     -- target pane, e.g. '%5'
    pane_pid             INTEGER,
    pane_count           INTEGER NOT NULL DEFAULT 1,
    foreground           TEXT,                     -- pane_current_command of the target pane
    activity_at          REAL,                     -- tmux window_activity of the target pane
    project_dir          TEXT,                     -- container path
    last_message         TEXT,                     -- last assistant message (truncated)
    pending_json         TEXT,                     -- pending approval request (JSON) or NULL
    agent_session_id     TEXT,                     -- Claude session id or Codex thread id
    transcript_path      TEXT,                     -- container path of the agent transcript
    initial_prompt       TEXT,
    meta_json            TEXT,                     -- launch options and adapter extras (JSON)
    created_at           REAL NOT NULL,
    updated_at           REAL NOT NULL,
    ended_at             REAL,
    end_reason           TEXT                      -- 'ended_by_user' | 'closed' | 'agent_exited'
                                                   -- | 'container_stopped' | 'server_restarted' | 'container_restarted'
);
CREATE INDEX IF NOT EXISTS sessions_env_status ON sessions(env, status);
CREATE INDEX IF NOT EXISTS sessions_pane ON sessions(env, socket_path, server_pid, pane_id);

CREATE TABLE IF NOT EXISTS activity (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT,
    env         TEXT,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT,
    attention   INTEGER NOT NULL DEFAULT 0,
    read_at     REAL,
    dedupe_key  TEXT UNIQUE,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS activity_created ON activity(created_at);
CREATE INDEX IF NOT EXISTS activity_session ON activity(session_id, created_at);

CREATE TABLE IF NOT EXISTS hook_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    env          TEXT NOT NULL,
    source       TEXT NOT NULL,                    -- 'claude' | 'codex'
    event        TEXT NOT NULL,                    -- hook_event_name or notify type
    session_id   TEXT,                             -- resolved dashboard session id (may be NULL)
    received_at  REAL NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS hook_events_received ON hook_events(received_at);

CREATE TABLE IF NOT EXISTS auth_sessions (
    id          TEXT PRIMARY KEY,                  -- sha256 of the cookie value
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    user_agent  TEXT
);

CREATE TABLE IF NOT EXISTS previews (
    id            TEXT PRIMARY KEY,
    env           TEXT NOT NULL,
    name          TEXT NOT NULL,
    project_dir   TEXT NOT NULL,                   -- container path
    command       TEXT NOT NULL,                   -- dev server command (run by bash -lc inside the container)
    port          INTEGER NOT NULL,                -- container port
    health_path   TEXT NOT NULL DEFAULT '/',
    test_command  TEXT,                            -- optional configured test command
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    UNIQUE(env, name)
);

CREATE TABLE IF NOT EXISTS preview_servers (
    id              TEXT PRIMARY KEY,
    preview_id      TEXT NOT NULL REFERENCES previews(id) ON DELETE CASCADE,
    status          TEXT NOT NULL,                 -- 'starting' | 'ready' | 'failed' | 'stopped' | 'exited'
    socket_path     TEXT NOT NULL,
    server_pid      INTEGER,
    tmux_session_id TEXT,
    tmux_created    INTEGER,
    started_at      REAL NOT NULL,
    ready_at        REAL,
    stopped_at      REAL,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS check_runs (
    id           TEXT PRIMARY KEY,
    preview_id   TEXT REFERENCES previews(id) ON DELETE SET NULL,
    kind         TEXT NOT NULL,                    -- 'smoke' | 'test_command'
    status       TEXT NOT NULL,                    -- 'running' | 'passed' | 'failed' | 'error'
    target_url   TEXT,
    summary      TEXT,
    result_json  TEXT,
    started_at   REAL NOT NULL,
    finished_at  REAL
);
CREATE INDEX IF NOT EXISTS check_runs_preview ON check_runs(preview_id, started_at);

-- tmux clients started by the terminal bridge. A podman exec session keeps
-- running in the container when the local podman client dies, so relay records
-- each client's container PID and sends it SIGHUP on disconnect and at startup.
CREATE TABLE IF NOT EXISTS terminal_clients (
    id              TEXT PRIMARY KEY,
    env             TEXT NOT NULL,
    container_pid   INTEGER,
    socket_path     TEXT NOT NULL,
    tmux_session_id TEXT NOT NULL,
    session_id      TEXT,
    started_at      REAL NOT NULL
);

-- The primary app of one project group on the Previews page. A row names a
-- candidate from the current inventory; it never stores a URL. The snapshot
-- is display text only and never authorizes a link or a server action.
CREATE TABLE IF NOT EXISTS preview_app_choices (
    env            TEXT NOT NULL,
    project_dir    TEXT NOT NULL,                  -- group folder (container path)
    kind           TEXT NOT NULL,                  -- 'registered' | 'process' | 'docker'
    preview_id     TEXT REFERENCES previews(id) ON DELETE SET NULL,  -- Remove keeps the choice (shown Stopped)
    identity_json  TEXT NOT NULL,                  -- the candidate identity that must match again
    snapshot_json  TEXT NOT NULL,                  -- display only
    chosen_at      REAL NOT NULL,
    PRIMARY KEY (env, project_dir)
);
"""


def now() -> float:
    return time.time()


def new_id() -> str:
    return uuid.uuid4().hex


def dumps(value: Any) -> str | None:
    return None if value is None else json.dumps(value, separators=(",", ":"), sort_keys=True)


def loads(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


class Database:
    """A small thread-safe wrapper around one SQLite connection."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- primitives ---------------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    @contextmanager
    def tx(self) -> Iterator["Database"]:
        """Run several statements atomically (``BEGIN IMMEDIATE`` ... ``COMMIT``)."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # -- generic row helpers -------------------------------------------------
    def insert(self, table: str, fields: dict[str, Any]) -> None:
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        self.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(fields.values()))

    def update(self, table: str, row_id: str, fields: dict[str, Any], *, key: str = "id") -> int:
        if not fields:
            return 0
        sets = ", ".join(f"{k} = ?" for k in fields)
        cur = self.execute(f"UPDATE {table} SET {sets} WHERE {key} = ?", (*fields.values(), row_id))
        return cur.rowcount

    # -- sessions ------------------------------------------------------------
    def get_session(self, session_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM sessions WHERE id = ?", (session_id,))

    def list_sessions(self, *, env: str | None = None, statuses: Sequence[str] | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM sessions WHERE 1=1"
        params: list[Any] = []
        if env is not None:
            sql += " AND env = ?"
            params.append(env)
        if statuses:
            sql += f" AND status IN ({', '.join('?' for _ in statuses)})"
            params.extend(statuses)
        sql += " ORDER BY created_at"
        return self.query(sql, params)

    def insert_session(self, fields: dict[str, Any]) -> str:
        row = dict(fields)
        row.setdefault("id", new_id())
        ts = now()
        row.setdefault("created_at", ts)
        row.setdefault("updated_at", ts)
        self.insert("sessions", row)
        return row["id"]

    def update_session(self, session_id: str, **fields: Any) -> int:
        fields["updated_at"] = now()
        return self.update("sessions", session_id, fields)

    def find_live_session_by_pane(self, env: str, socket_path: str, server_pid: int | None, pane_id: str) -> sqlite3.Row | None:
        """Map a hook event's ``$TMUX``/``$TMUX_PANE`` to a live dashboard session."""
        sql = "SELECT * FROM sessions WHERE status = 'live' AND env = ? AND socket_path = ? AND pane_id = ?"
        params: list[Any] = [env, socket_path, pane_id]
        if server_pid is not None:
            sql += " AND server_pid = ?"
            params.append(server_pid)
        sql += " ORDER BY updated_at DESC LIMIT 1"
        return self.one(sql, params)


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else {k: row[k] for k in row.keys()}
