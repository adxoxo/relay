"""Claude Code adapter: map hook events to session state and answer approvals.

The event names and fields come from the Claude Code hooks reference. relay
receives a trimmed payload (see ``adapters/hook_script.py``) plus the wrapper
fields ``ts``, ``relay_session_id``, and the tmux identity.

State machine (``state`` with ``state_source='hook'``):

- SessionStart       -> idle, record agent_session_id and transcript_path
- UserPromptSubmit   -> working, clear pending
- PreToolUse         -> working, unless already needs_input
- PermissionRequest  -> needs_input, set pending, attention 'needs_input'
- PostToolUse        -> working, clear pending, tool activity for file edits
- PostToolUseFailure -> working, clear pending, error/tool activity
- PermissionDenied   -> activity only
- Notification       -> permission_prompt: needs_input + attention;
                        idle_prompt: idle
- Stop               -> idle, last_message, clear pending, 'turn_complete'
- StopFailure        -> idle, 'error' activity
- SessionEnd         -> exited

Every event except SessionStart is ignored when older than the row's
``state_at`` (compared on the event ts), so a late file never rewinds state.
"""

from __future__ import annotations

import math
import sqlite3
from typing import TYPE_CHECKING, Any

from .. import activity, screen
from ..db import dumps, loads, new_id
from ..errors import ApiError

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime import Runtime

# States
IDLE = "idle"
WORKING = "working"
NEEDS_INPUT = "needs_input"
EXITED = "exited"

_FILE_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit"})
ACTIVE_TURN_KEY = "claude_active_turn"


def _turn_metadata(row: sqlite3.Row, payload: dict[str, Any], event_ts: float, *, start: bool) -> str | None:
    """Keep the immutable submit identity separate from mutable state_at."""
    meta = loads(row["meta_json"])
    meta = dict(meta) if isinstance(meta, dict) else {}
    meta.pop(ACTIVE_TURN_KEY, None)
    if start and math.isfinite(event_ts) and event_ts > 0:
        prompt = payload.get("prompt")
        meta[ACTIVE_TURN_KEY] = {
            "agent_session_id": payload.get("session_id") or row["agent_session_id"],
            "transcript_path": payload.get("transcript_path") or row["transcript_path"],
            "started_at": event_ts,
            "after": _row_state_at(row) if row["state"] in (IDLE, EXITED) else None,
            "prompt": prompt.strip()[:2000] if isinstance(prompt, str) else None,
        }
    return dumps(meta)


def summarize_tool(name: str, tool_input: Any) -> tuple[str, list[str]]:
    """Return a short summary and the screen match strings for a tool call.

    The match strings are what a screen capture must show before relay will
    type an approval answer. An empty list means relay cannot verify the
    on-screen prompt, so the request is not answerable.
    """
    if not isinstance(tool_input, dict):
        return name, []
    if name in ("Bash", "PowerShell"):
        command = tool_input.get("command")
        if isinstance(command, str) and command.strip():
            trimmed = command.strip()
            return trimmed[:200], [trimmed[:80]]
        return name, []
    if name in _FILE_EDIT_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("path")
        if isinstance(path, str) and path.strip():
            base = path.strip().rsplit("/", 1)[-1]
            return f"{name} {path.strip()}"[:200], [base]
        return name, []
    if name in ("WebFetch",):
        url = tool_input.get("url")
        if isinstance(url, str) and url.strip():
            return url.strip()[:200], [url.strip()[:80]]
        return name, []
    for key in ("pattern", "query", "path", "description"):
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:200], [val.strip()[:60]]
    return name, []


def _row_state_at(row: sqlite3.Row) -> float | None:
    try:
        value = row["state_at"]
    except (IndexError, KeyError):
        return None
    return float(value) if value is not None else None


def _relative_path(path: str) -> str:
    if not isinstance(path, str):
        return ""
    stripped = path.strip()
    for root in ("/home/node/projects/", "/home/node/"):
        if stripped.startswith(root):
            return stripped[len(root):]
    return stripped


def _pending(row: sqlite3.Row) -> dict[str, Any] | None:
    try:
        raw = row["pending_json"]
    except (IndexError, KeyError):
        return None
    parsed = loads(raw)
    return parsed if isinstance(parsed, dict) else None


def _set_state(rt: "Runtime", session_id: str, state: str, event_ts: float, **fields: Any) -> None:
    rt.db.update_session(session_id, state=state, state_source="hook", state_at=event_ts, **fields)


def _session_ids(row: sqlite3.Row, payload: dict[str, Any]) -> dict[str, Any]:
    """``agent_session_id`` and ``transcript_path`` from an event, when they changed."""
    fields: dict[str, Any] = {}
    agent_id = payload.get("session_id")
    if isinstance(agent_id, str) and agent_id and agent_id != row["agent_session_id"]:
        fields["agent_session_id"] = agent_id
    transcript = payload.get("transcript_path")
    if isinstance(transcript, str) and transcript and transcript != row["transcript_path"]:
        fields["transcript_path"] = transcript
    return fields


def apply_event(rt: "Runtime", row: sqlite3.Row, event: dict[str, Any]) -> None:
    """Apply one Claude hook event to the session ``row``."""
    payload = event.get("payload") or {}
    name = payload.get("hook_event_name")
    event_ts = float(event.get("ts") or 0.0)
    session_id = row["id"]
    env = row["env"]

    if not name:
        return

    if name != "SessionStart":
        state_at = _row_state_at(row)
        if state_at is not None and event_ts and event_ts < state_at:
            return  # a late event never rewinds state
        # Every Claude hook event carries session_id and transcript_path. A
        # session that started before relay's hooks were installed never sent
        # SessionStart, so take them from the first event that arrives.
        ids = _session_ids(row, payload)
        if ids:
            rt.db.update_session(session_id, **ids)

    if name == "SessionStart":
        fields: dict[str, Any] = {}
        agent_id = payload.get("session_id")
        if isinstance(agent_id, str) and agent_id:
            fields["agent_session_id"] = agent_id
        transcript = payload.get("transcript_path")
        if isinstance(transcript, str) and transcript:
            fields["transcript_path"] = transcript
        # A session that just started has no open permission prompt. This also
        # clears a folder-trust request that relay raised from the screen.
        _set_state(rt, session_id, IDLE, event_ts, pending_json=None,
                   meta_json=_turn_metadata(row, payload, event_ts, start=False), **fields)

    elif name == "UserPromptSubmit":
        _set_state(rt, session_id, WORKING, event_ts, pending_json=None,
                   meta_json=_turn_metadata(row, payload, event_ts, start=True))

    elif name == "PreToolUse":
        if row["state"] != NEEDS_INPUT:
            _set_state(rt, session_id, WORKING, event_ts)

    elif name == "PermissionRequest":
        tool = payload.get("tool_name") or "tool"
        tool_input = payload.get("tool_input")
        summary, match = summarize_tool(tool, tool_input)
        detail = tool_input.get("description") if isinstance(tool_input, dict) else None
        pending = {
            "id": new_id()[:16],
            "source": "claude",
            "tool": tool,
            "summary": summary,
            "detail": detail if isinstance(detail, str) else None,
            "match": match,
            "created_at": event_ts,
            "answerable": bool(match),
        }
        _set_state(rt, session_id, NEEDS_INPUT, event_ts, pending_json=dumps(pending))
        activity.record(
            rt.db,
            kind="needs_input",
            title=f"Needs approval: {tool}",
            body=summary,
            session_id=session_id,
            env=env,
            attention=True,
            bus=rt.bus,
        )

    elif name in ("PostToolUse", "PostToolUseFailure"):
        _set_state(rt, session_id, WORKING, event_ts, pending_json=None)
        tool = payload.get("tool_name") or "tool"
        tool_input = payload.get("tool_input")
        if name == "PostToolUseFailure":
            activity.record(
                rt.db,
                kind="error",
                title=f"Tool failed: {tool}",
                body=None,
                session_id=session_id,
                env=env,
                attention=True,
                bus=rt.bus,
            )
        elif tool in _FILE_EDIT_TOOLS and isinstance(tool_input, dict):
            path = tool_input.get("file_path") or tool_input.get("path")
            rel = _relative_path(path) if isinstance(path, str) else ""
            if rel:
                activity.record(
                    rt.db,
                    kind="tool",
                    title=f"Edited {rel}",
                    session_id=session_id,
                    env=env,
                    bus=rt.bus,
                )

    elif name == "PermissionDenied":
        tool = payload.get("tool_name") or "tool"
        activity.record(
            rt.db,
            kind="tool",
            title=f"Denied: {tool}",
            session_id=session_id,
            env=env,
            bus=rt.bus,
        )

    elif name == "Notification":
        ntype = payload.get("notification_type")
        if ntype == "permission_prompt":
            existing = _pending(row)
            if existing is None:
                pending = {
                    "id": new_id()[:16],
                    "source": "claude",
                    "tool": "tool",
                    "summary": payload.get("message") or "Claude needs your permission",
                    "detail": None,
                    "match": [],
                    "created_at": event_ts,
                    "answerable": False,
                }
                _set_state(rt, session_id, NEEDS_INPUT, event_ts, pending_json=dumps(pending))
            else:
                _set_state(rt, session_id, NEEDS_INPUT, event_ts)
            activity.record(
                rt.db,
                kind="needs_input",
                title="Needs your attention",
                body=payload.get("message"),
                session_id=session_id,
                env=env,
                attention=True,
                dedupe_key=f"perm-notif:{session_id}:{event_ts}",
                bus=rt.bus,
            )
        elif ntype == "idle_prompt":
            _set_state(rt, session_id, IDLE, event_ts, meta_json=_turn_metadata(row, payload, event_ts, start=False))

    elif name == "Stop":
        last = payload.get("last_assistant_message")
        fields = {"pending_json": None, "meta_json": _turn_metadata(row, payload, event_ts, start=False)}
        if isinstance(last, str) and last:
            fields["last_message"] = last
        _set_state(rt, session_id, IDLE, event_ts, **fields)
        prompt_id = payload.get("prompt_id")
        dedupe = f"turn:{session_id}:{prompt_id}" if prompt_id else f"turn:{session_id}:{event_ts}"
        activity.record(
            rt.db,
            kind="turn_complete",
            title="Turn complete",
            body=last if isinstance(last, str) else None,
            session_id=session_id,
            env=env,
            attention=True,
            dedupe_key=dedupe,
            bus=rt.bus,
        )

    elif name == "StopFailure":
        _set_state(rt, session_id, IDLE, event_ts, meta_json=_turn_metadata(row, payload, event_ts, start=False))
        activity.record(
            rt.db,
            kind="error",
            title="Turn failed",
            body=payload.get("error") if isinstance(payload.get("error"), str) else None,
            session_id=session_id,
            env=env,
            attention=True,
            bus=rt.bus,
        )

    elif name == "SessionEnd":
        _set_state(rt, session_id, EXITED, event_ts, meta_json=_turn_metadata(row, payload, event_ts, start=False))


async def answer_permission(rt: "Runtime", session_id: str, request_id: str, decision: str) -> None:
    """Answer a pending Claude permission request by typing into its pane.

    The screen must agree with the recorded request before relay types, so an
    agent message can never be mistaken for an approval dialog. ``allow`` sends
    ``Enter`` only when option 1 ("Yes") is highlighted; ``deny`` sends
    ``Escape``.
    """
    async with rt.lock_for(session_id):
        row = rt.db.get_session(session_id)
        if row is None:
            raise ApiError(404, "not_found", "Session not found")
        pending = _pending(row)
        if pending is None:
            raise ApiError(409, "no_pending_request", "No pending approval for this session")
        if pending.get("id") != request_id:
            raise ApiError(409, "stale_request", "The pending request changed")

        socket = row["socket_path"]
        pane_id = row["pane_id"]
        if not pane_id:
            raise ApiError(409, "prompt_not_visible", "No target pane for this session")

        tmux = rt.tmux_for(row["env"])
        capture = await tmux.capture(socket, pane_id, lines=80)
        match = pending.get("match") or []
        if not screen.permission_dialog_visible(capture) or not screen.screen_mentions(capture, match):
            raise ApiError(409, "prompt_not_visible", "The approval prompt is not visible on screen")

        if decision == "allow":
            if not screen.claude_yes_highlighted(capture):
                raise ApiError(
                    409,
                    "prompt_not_visible",
                    "The Yes option is not highlighted; open the prompt in the terminal first",
                )
            await tmux.send_keys(socket, pane_id, "Enter")
            new_state = WORKING
            title = "Approved request"
        elif decision == "deny":
            await tmux.send_keys(socket, pane_id, "Escape")
            new_state = WORKING
            title = "Denied request"
        else:
            raise ApiError(400, "invalid_request", "decision must be 'allow' or 'deny'")

        rt.db.update_session(session_id, pending_json=None, state=new_state, state_source="hook")
        activity.record(
            rt.db,
            kind="tool",
            title=title,
            body=pending.get("summary"),
            session_id=session_id,
            env=row["env"],
            attention=False,
            bus=rt.bus,
        )
        rt.bus.publish("sessions", "activity")
