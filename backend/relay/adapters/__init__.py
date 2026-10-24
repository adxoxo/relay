"""Agent event dispatch.

``handle_event`` is called by the inbox loop once per event file. It resolves
the event to a live dashboard session, stores a ``hook_events`` row, dispatches
to the Claude or Codex adapter, and publishes bus topics so browsers refetch.

Session resolution (the env always comes from the inbox location):

1. If the event carries ``relay_session_id`` and a live row with that id exists
   in the same env, use it. This is the reliable path for dashboard sessions.
2. Otherwise parse ``$TMUX`` (``socket,server_pid,index``) and ``$TMUX_PANE``
   (``%N``) and look up a live row by pane, for a laptop-started session that
   opted in through ``relay hooks install``.

Codex events pass the same identity to ``codex.resolve``. It uses only an
explicit argv relay id; other notify events resolve by their exact thread link
(see ``adapters/codex.py``). ``internal=True`` marks lifecycle events from
relay's own rollout reader; inbox events are never internal.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

from ..db import dumps, now
from . import claude, codex

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime import Runtime

__all__ = ["handle_event", "claude", "codex"]


def _parse_tmux(tmux: Any) -> tuple[str, int | None] | None:
    """Parse ``$TMUX`` = ``socket,server_pid,index`` into (socket, server_pid)."""
    if not isinstance(tmux, str) or not tmux:
        return None
    parts = tmux.split(",")
    if len(parts) < 2:
        return None
    socket = parts[0]
    if not socket:
        return None
    try:
        server_pid: int | None = int(parts[1])
    except (TypeError, ValueError):
        server_pid = None
    return socket, server_pid


def _resolve_session(rt: "Runtime", env_id: str, event: dict[str, Any]) -> sqlite3.Row | None:
    relay_session_id = event.get("relay_session_id")
    if isinstance(relay_session_id, str) and relay_session_id:
        row = rt.db.get_session(relay_session_id)
        if row is not None and row["env"] == env_id and row["status"] == "live":
            return row

    parsed = _parse_tmux(event.get("tmux"))
    pane = event.get("tmux_pane")
    if parsed is not None and isinstance(pane, str) and pane:
        socket, server_pid = parsed
        return rt.db.find_live_session_by_pane(env_id, socket, server_pid, pane)
    return None


def _identity(rt: "Runtime", env_id: str, event: dict[str, Any]) -> tuple[sqlite3.Row | None, str | None]:
    """Live row named by the event's relay id or tmux pane, and how it was named.

    ``via`` is ``argv`` (relay id from the notify argv), ``env`` (relay id from
    the environment), or ``pane``. An argv relay id that names no live row in
    this env is final: the pane is not tried.
    """
    relay_session_id = event.get("relay_session_id")
    via = "argv" if event.get("relay_id_from") == "argv" else "env"
    if isinstance(relay_session_id, str) and relay_session_id:
        row = rt.db.get_session(relay_session_id)
        if row is not None and row["env"] == env_id and row["status"] == "live":
            return row, via
        if via == "argv":
            return None, "argv"

    parsed = _parse_tmux(event.get("tmux"))
    pane = event.get("tmux_pane")
    if parsed is not None and isinstance(pane, str) and pane:
        socket, server_pid = parsed
        row = rt.db.find_live_session_by_pane(env_id, socket, server_pid, pane)
        if row is not None:
            return row, "pane"
    return None, None


def _event_name(event: dict[str, Any]) -> str:
    payload = event.get("payload") or {}
    if event.get("source") == "codex":
        return str(payload.get("type") or "notify")
    return str(payload.get("hook_event_name") or "unknown")


async def handle_event(rt: "Runtime", env_id: str, event: dict[str, Any], *, internal: bool = False) -> None:
    """Resolve, record, and dispatch one agent event."""
    if not isinstance(event, dict):
        return
    source = event.get("source")
    if source == "codex":
        identity = (None, None) if internal else _identity(rt, env_id, event)
        row = codex.resolve(rt, env_id, event, internal=internal, identity=identity)
    else:
        row = _resolve_session(rt, env_id, event)

    rt.db.insert(
        "hook_events",
        {
            "env": env_id,
            "source": source or "unknown",
            "event": _event_name(event),
            "session_id": row["id"] if row is not None else None,
            "received_at": now(),
            "payload_json": dumps(event) or "{}",
        },
    )

    if row is None:
        return

    if source == "claude":
        claude.apply_event(rt, row, event)
    elif source == "codex":
        codex.apply(rt, row, event, internal=internal)

    rt.bus.publish("sessions", "activity")
