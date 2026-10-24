"""Activity feed and attention inbox.

An activity item with ``attention=True`` is an inbox item (a request for the
user's attention). Items are deduplicated by ``dedupe_key``, so the same
agent event reported twice produces one item.

Kinds used by relay modules:

- ``needs_input``      agent waits for a permission answer or other input (attention)
- ``turn_complete``    agent finished a turn and waits for the next prompt (attention)
- ``session_created``  a session started (from the dashboard or discovered)
- ``session_ended``    a session ended
- ``session_interrupted`` a session was lost to a container or tmux server restart (attention)
- ``tool``             an agent tool call (file edit, command)
- ``error``            an agent or relay error (attention)
- ``preview``          a preview server changed state
- ``check``            a browser check or test run finished (attention when it failed)
"""

from __future__ import annotations

import sqlite3
from typing import Any, Sequence

from .bus import Bus
from .db import Database, now

ATTENTION_KINDS = frozenset({"needs_input", "turn_complete", "session_interrupted", "error"})
MAX_TITLE = 200
MAX_BODY = 2000


def _item(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "env": row["env"],
        "kind": row["kind"],
        "title": row["title"],
        "body": row["body"],
        "attention": bool(row["attention"]),
        "read": row["read_at"] is not None,
        "created_at": row["created_at"],
    }


def record(
    db: Database,
    *,
    kind: str,
    title: str,
    body: str | None = None,
    session_id: str | None = None,
    env: str | None = None,
    attention: bool | None = None,
    dedupe_key: str | None = None,
    bus: Bus | None = None,
) -> int | None:
    """Insert an activity item. Returns its id, or ``None`` when deduplicated."""
    if attention is None:
        attention = kind in ATTENTION_KINDS
    try:
        cur = db.execute(
            "INSERT INTO activity (session_id, env, kind, title, body, attention, dedupe_key, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                env,
                kind,
                title[:MAX_TITLE],
                body[:MAX_BODY] if body else None,
                1 if attention else 0,
                dedupe_key,
                now(),
            ),
        )
    except sqlite3.IntegrityError:
        return None
    if bus is not None:
        bus.publish("activity")
    return cur.lastrowid


def list_items(
    db: Database,
    *,
    limit: int = 50,
    before_id: int | None = None,
    attention_only: bool = False,
    unread_only: bool = False,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM activity WHERE 1=1"
    params: list[Any] = []
    if before_id is not None:
        sql += " AND id < ?"
        params.append(before_id)
    if attention_only:
        sql += " AND attention = 1"
    if unread_only:
        sql += " AND read_at IS NULL"
    if session_id is not None:
        sql += " AND session_id = ?"
        params.append(session_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    return [_item(r) for r in db.query(sql, params)]


def unread_count(db: Database, *, kind: str | None = None) -> int:
    sql = "SELECT COUNT(*) AS n FROM activity WHERE attention = 1 AND read_at IS NULL"
    params: list[Any] = []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    row = db.one(sql, params)
    return int(row["n"]) if row else 0


def mark_read(
    db: Database,
    *,
    ids: Sequence[int] | None = None,
    session_id: str | None = None,
    all_items: bool = False,
    bus: Bus | None = None,
) -> int:
    ts = now()
    if all_items:
        cur = db.execute("UPDATE activity SET read_at = ? WHERE read_at IS NULL", (ts,))
    elif session_id is not None:
        cur = db.execute("UPDATE activity SET read_at = ? WHERE read_at IS NULL AND session_id = ?", (ts, session_id))
    elif ids:
        marks = ", ".join("?" for _ in ids)
        cur = db.execute(f"UPDATE activity SET read_at = ? WHERE read_at IS NULL AND id IN ({marks})", (ts, *ids))
    else:
        return 0
    if cur.rowcount and bus is not None:
        bus.publish("activity")
    return cur.rowcount


def prune(db: Database, *, activity_days: int = 30, hook_event_days: int = 3) -> None:
    ts = now()
    db.execute("DELETE FROM activity WHERE created_at < ?", (ts - activity_days * 86400,))
    db.execute("DELETE FROM hook_events WHERE received_at < ?", (ts - hook_event_days * 86400,))
