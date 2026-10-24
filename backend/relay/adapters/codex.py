"""Codex adapter: map Codex notify and rollout lifecycle events to session state.

Two sources reach this adapter through ``adapters.handle_event``:

- notify: Codex runs ``python3 hook.py codex [--relay-session-id=ID] '<json>'``
  when a turn ends. Fields: ``type`` ('agent-turn-complete'), ``thread-id``,
  ``turn-id``, ``cwd``, ``input-messages``, ``last-assistant-message``. In
  Work 2 the shared app-server daemon runs it, so the event has no tmux
  environment; a relay-started Codex carries its relay id in the argv.
- rollout lifecycle (``internal=True``, from ``codex_runtime.poll_env`` only):
  ``task_started``, ``task_complete``, ``turn_aborted`` of a verified thread
  link. These types are never accepted from the inbox.

Resolution of a notify event (``resolve``), the env always comes from the inbox:

1. An explicit argv relay id names a live Codex row; an argv id that names no
   live Codex row stops here. A verified link on that row must name the
   event's thread; another top-level thread marks the link stale, because the
   TUI switched threads. An unlinked row is linked when the Codex state index
   and the rollout metadata prove a top-level thread; a subagent or unknown
   thread fails closed.
2. Otherwise the exact thread id names exactly one live Codex row with a
   verified link. Zero or several rows: unmatched. A thread is never adopted
   here; only fresh native evidence of the pane (``codex_runtime``) moves a
   link to another thread. A
   relay id from the notify environment or a tmux pane is not used: the shared
   daemon runs notify with its own environment, which can name the session
   that started the daemon instead of this thread's pane.

State: the rollout reader owns the state of every linked row.
``task_started`` -> working; ``task_complete`` -> idle with a
``turn_complete`` activity; ``turn_aborted`` -> idle. A notify completion only
records the same activity (same dedupe key per turn, so both sources record it
once). It never sets idle: its time is when the hook ran, which can be after
the next turn started. Codex's thread-title turn ends quietly: its rollout
completion (``title-turn``, set only by relay's rollout reader) gives idle with
no Activity and no last message, and a title notify changes nothing. An event
older than the state of an event-owned row is ignored, so a later prompt or
interrupt is never undone by a late event. A bootstrap event (latest state
read from the rollout tail) records no activity. A Codex approval dialog on
screen shows as needs_input through ``codex_runtime._screen_overlay``; a newer
rollout event removes that overlay.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import TYPE_CHECKING, Any

from .. import activity, codex_runtime
from ..codex_runtime import EVENT_SOURCES, LIFECYCLE_TYPES, SCREEN_PENDING_SOURCE, STATE_SOURCE, UUID_RE, BindingError
from ..db import loads

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime import Runtime

log = logging.getLogger("relay.adapters.codex")

IDLE = "idle"
WORKING = "working"
NOTIFY_COMPLETE = "agent-turn-complete"
# An event this much older than the row's creation predates the session.
_CREATE_SKEW = 5.0


def _row_state_at(row: sqlite3.Row) -> float | None:
    try:
        value = row["state_at"]
    except (IndexError, KeyError):
        return None
    return float(value) if value is not None else None


def _is_title_turn(last: Any) -> bool:
    """True for Codex's thread-title turn, whose message is ``{"title": "..."}``."""
    if not isinstance(last, str) or not last.lstrip().startswith("{"):
        return False
    try:
        parsed = json.loads(last)
    except ValueError:
        return False
    return isinstance(parsed, dict) and set(parsed) == {"title"} and isinstance(parsed["title"], str)


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def _thread_id(payload: dict[str, Any]) -> str | None:
    tid = payload.get("thread-id")
    return tid if isinstance(tid, str) and UUID_RE.match(tid) else None


def _ts(event: dict[str, Any]) -> float:
    try:
        return float(event.get("ts") or 0.0)
    except (TypeError, ValueError):
        return 0.0


# -- resolution -------------------------------------------------------------------


def resolve(
    rt: "Runtime",
    env_id: str,
    event: dict[str, Any],
    *,
    internal: bool,
    identity: tuple[sqlite3.Row | None, str | None],
) -> sqlite3.Row | None:
    """The live Codex row this event belongs to, or ``None`` (recorded unmatched)."""
    payload = _payload(event)
    thread_id = _thread_id(payload)
    if thread_id is None:
        return None
    if internal:
        return _resolve_internal(rt, env_id, event, thread_id)
    if event.get("origin") == STATE_SOURCE or payload.get("type") in LIFECYCLE_TYPES:
        return None  # lifecycle events come only from relay's own rollout reader
    row, via = identity
    if via == "argv":
        if row is None or row["kind"] != "codex":
            return None
        return _check_identity(rt, row, thread_id, _ts(event))
    # A relay id from the environment or a tmux pane is not evidence here: the
    # shared daemon runs notify with its own environment. Only the exact link decides.
    return _resolve_by_thread(rt, env_id, thread_id)


def _resolve_internal(rt: "Runtime", env_id: str, event: dict[str, Any], thread_id: str) -> sqlite3.Row | None:
    rid = event.get("relay_session_id")
    row = rt.db.get_session(rid) if isinstance(rid, str) and rid else None
    if row is None or row["env"] != env_id or row["status"] != "live" or row["kind"] != "codex":
        return None
    binding = codex_runtime.binding_of(row)
    if not codex_runtime.is_verified(binding) or binding["thread_id"] != thread_id:
        return None
    return row


def _check_identity(rt: "Runtime", row: sqlite3.Row, thread_id: str, ts: float) -> sqlite3.Row | None:
    """The row named by an explicit argv relay id, linked to the thread if unlinked."""
    created = row["created_at"]
    if ts and created and ts < float(created) - _CREATE_SKEW:
        return None  # older than the session: never adopt an identity from it
    binding = codex_runtime.binding_of(row)
    if codex_runtime.is_verified(binding):
        if binding["thread_id"] == thread_id:
            return row
        codex_runtime.note_thread_conflict(rt, row, thread_id)
        return None
    if binding is not None:
        return None  # a stale link is never relinked automatically
    try:
        codex_runtime.bind_from_launch(rt, row, thread_id, via="argv")
    except (BindingError, OSError) as exc:
        log.info("Codex event for %s not linked to thread %s: %s", row["id"], thread_id, exc)
        return None
    return rt.db.get_session(row["id"])


def _resolve_by_thread(rt: "Runtime", env_id: str, thread_id: str) -> sqlite3.Row | None:
    rows = rt.db.query(
        "SELECT * FROM sessions WHERE status = 'live' AND env = ? AND kind = 'codex' AND agent_session_id = ?",
        (env_id, thread_id),
    )
    if len(rows) != 1:
        return None  # unknown or ambiguous
    binding = codex_runtime.binding_of(rows[0])
    if not codex_runtime.is_verified(binding) or binding["thread_id"] != thread_id:
        return None
    return rows[0]


# -- state ------------------------------------------------------------------------


def apply(rt: "Runtime", row: sqlite3.Row, event: dict[str, Any], *, internal: bool) -> None:
    """Apply one resolved Codex event to ``row``."""
    ptype = _payload(event).get("type")
    if internal:
        if ptype in LIFECYCLE_TYPES:
            apply_lifecycle(rt, row, event)
    elif ptype == NOTIFY_COMPLETE:
        apply_notify(rt, row, event)


def _record_turn(rt: "Runtime", row: sqlite3.Row, turn_id: Any, last: Any, event_ts: float) -> None:
    session_id = row["id"]
    dedupe = f"turn:{session_id}:{turn_id}" if turn_id else f"turn:{session_id}:{event_ts}"
    activity.record(
        rt.db,
        kind="turn_complete",
        title="Turn complete",
        body=last if isinstance(last, str) else None,
        session_id=session_id,
        env=row["env"],
        attention=True,
        dedupe_key=dedupe,
        bus=rt.bus,
    )


def apply_notify(rt: "Runtime", row: sqlite3.Row, event: dict[str, Any]) -> None:
    """Record the turn activity of one notify completion on the linked ``row``.

    ``resolve`` returns only rows with a verified link, and the rollout reader
    owns their state. The notify time is when the hook ran, which can be after
    the next turn started, so an old completion must not set idle here.
    """
    payload = _payload(event)
    if payload.get("type") != NOTIFY_COMPLETE:
        return

    last = payload.get("last-assistant-message")
    if _is_title_turn(last):
        # Codex runs a separate model turn to name the thread and reports it as
        # another agent-turn-complete (seen live with codex-cli 0.159). It is not
        # the user's turn: it must not change state, last_message, or the inbox.
        return

    _record_turn(rt, row, payload.get("turn-id"), last, _ts(event))


def apply_lifecycle(rt: "Runtime", row: sqlite3.Row, event: dict[str, Any]) -> None:
    """Apply one rollout lifecycle event of the row's verified thread."""
    payload = _payload(event)
    ptype = payload.get("type")
    event_ts = _ts(event)
    if not event_ts:
        return
    state_at = _row_state_at(row)
    if row["state_source"] in EVENT_SOURCES and state_at is not None and event_ts < state_at:
        return  # a newer prompt, interrupt, or event already set the state

    fields: dict[str, Any] = {}
    last = payload.get("last-assistant-message")
    # The rollout reader marks the completion of Codex's thread-title turn. The
    # cursor already applied the turn order guard, so this end belongs to the
    # turn that ran: it gives idle, but no last message and no Activity.
    title = ptype == "task_complete" and (payload.get("title-turn") is True or _is_title_turn(last))
    pending = loads(row["pending_json"])
    pending_source = pending.get("source") if isinstance(pending, dict) else None
    if pending_source == SCREEN_PENDING_SOURCE:
        # A rollout event newer than the approval overlay: the turn moved on,
        # so the dialog was answered.
        fields["pending_json"] = None
    if ptype == "task_started":
        state = WORKING
        if pending_source == "screen":
            fields["pending_json"] = None  # the folder trust question was answered
    elif ptype == "task_complete":
        state = IDLE
        if not title and isinstance(last, str) and last:
            fields["last_message"] = last
    elif ptype == "turn_aborted":
        state = IDLE
    else:
        return

    rt.db.update_session(row["id"], state=state, state_source=STATE_SOURCE, state_at=event_ts, **fields)
    if ptype == "task_complete" and not title and not event.get("bootstrap"):
        _record_turn(rt, row, payload.get("turn-id"), last, event_ts)
