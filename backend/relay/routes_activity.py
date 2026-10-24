"""HTTP routes for the activity feed, conversation view, and approvals.

All routes are under ``/api`` and protected by ``require_user``. Errors use the
shared ``ApiError`` shape ``{"error": code, "message": text}``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from . import activity, codex_runtime, terminal_history, transcript
from .adapters import claude
from .errors import ApiError
from .runtime import Runtime, get_rt, require_user
from .db import loads

router = APIRouter()
log = logging.getLogger("relay.conversation")


def _read_conversation(env: Any, source: terminal_history.Source, limit: int) -> list[dict[str, Any]]:
    if source.kind == "claude":
        return transcript.read_conversation(
            env, source.transcript_path, limit=limit, expected_session_id=source.agent_session_id,
        )
    binding = json.loads(source.binding_identity)
    proof = codex_runtime.verify_thread(env, source.agent_session_id, expected_path=source.transcript_path)
    if binding is None or proof["rollout"] != binding.get("rollout"):
        raise transcript.TranscriptError("rollout identity differs from the verified source")
    return codex_runtime.read_codex_conversation(
        env, source.transcript_path, limit=limit, expected_session_id=source.agent_session_id,
        expected_file_id=proof["rollout"],
    )


def _active_turn(
    rt: Runtime, row: Any, source: terminal_history.Source, items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Expose only immutable start evidence for the source checked after the read."""
    result: dict[str, Any] = {"active_started_at": None, "active_prompt_id": None}
    if row["status"] != "live" or row["state"] not in ("working", "needs_input"):
        return result
    if source.kind == "codex":
        cursor = rt.cache.get("codex_rollout_cursors", {}).get(row["id"])
        binding = json.loads(source.binding_identity)
        if (
            cursor is None or cursor.active_turn is None or cursor.env != row["env"]
            or cursor.thread_id != source.agent_session_id or cursor.container_path != source.transcript_path
            or {"dev": cursor.dev, "ino": cursor.ino} != binding.get("rollout")
        ):
            return result
        result["active_started_at"] = cursor.active_started_at
        if any(item["role"] == "user" and item["id"] == cursor.active_prompt_id for item in items):
            result["active_prompt_id"] = cursor.active_prompt_id
        return result
    meta = loads(row["meta_json"])
    turn = meta.get(claude.ACTIVE_TURN_KEY) if isinstance(meta, dict) else None
    if (
        row["state_source"] != "hook" or not isinstance(turn, dict)
        or turn.get("agent_session_id") != source.agent_session_id
        or turn.get("transcript_path") != source.transcript_path
    ):
        return result
    started = turn.get("started_at")
    if not isinstance(started, (int, float)) or isinstance(started, bool) or not math.isfinite(started) or started <= 0:
        return result
    result["active_started_at"] = started
    prompt = turn.get("prompt")
    latest = next((item for item in reversed(items) if item["role"] == "user"), None)
    if not isinstance(prompt, str) or not prompt or latest is None or latest["text"].strip()[:2000] != prompt:
        return result
    ts = latest.get("ts")
    after = turn.get("after")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool) or not math.isfinite(ts):
        return result
    # A prior completed boundary permits the transcript timestamp to precede
    # the submit hook, while still excluding the previous saved prompt.
    if isinstance(after, (int, float)) and not isinstance(after, bool) and math.isfinite(after):
        matches = ts > after
    else:
        matches = ts >= started
    if matches:
        result["active_prompt_id"] = latest["id"]
    return result


class ReadBody(BaseModel):
    ids: list[int] | None = None
    session_id: str | None = None
    all: bool = False


class ApprovalBody(BaseModel):
    request_id: str = Field(min_length=1)
    decision: str


def _bool_param(value: str | None) -> bool:
    return value in ("1", "true", "yes")


@router.get("/api/activity")
async def get_activity(
    request: Request,
    limit: int = 50,
    before_id: int | None = None,
    attention: str | None = None,
    unread: str | None = None,
    session_id: str | None = None,
    _: Any = Depends(require_user),
) -> dict[str, Any]:
    rt: Runtime = get_rt(request)
    items = activity.list_items(
        rt.db,
        limit=limit,
        before_id=before_id,
        attention_only=_bool_param(attention),
        unread_only=_bool_param(unread),
        session_id=session_id,
    )
    return {"items": items, "unread": activity.unread_count(rt.db)}


@router.post("/api/activity/read")
async def post_activity_read(
    request: Request,
    body: ReadBody,
    _: Any = Depends(require_user),
) -> dict[str, Any]:
    rt: Runtime = get_rt(request)
    updated = activity.mark_read(
        rt.db,
        ids=body.ids,
        session_id=body.session_id,
        all_items=body.all,
        bus=rt.bus,
    )
    return {"updated": updated}


@router.get("/api/sessions/{session_id}/conversation")
async def get_conversation(
    request: Request,
    session_id: str,
    limit: int = 200,
    _: Any = Depends(require_user),
) -> dict[str, Any]:
    rt: Runtime = get_rt(request)
    row = rt.db.get_session(session_id)
    if row is None:
        raise ApiError(404, "not_found", "Session not found")

    try:
        env = rt.settings.env(row["env"])
    except KeyError:
        raise ApiError(404, "unknown_env", "The session environment is unavailable.") from None

    try:
        source = terminal_history.conversation_source_for(row, env)
        items = await asyncio.wait_for(
            asyncio.to_thread(_read_conversation, env, source, max(1, min(limit, transcript.MAX_ITEMS))), timeout=15,
        )
        fresh = rt.db.get_session(session_id)
        if terminal_history.conversation_source_for(fresh, env) != source:
            return {"supported": False, "reason": "The session source changed. Refresh Conversation.", "items": []}
    except terminal_history.HistoryUnavailable as exc:
        return {"supported": False, "reason": exc.note, "items": []}
    except (transcript.TranscriptError, codex_runtime.BindingError, OSError, TimeoutError, ValueError, RecursionError) as exc:
        log.warning("conversation read refused for session %s: %s", session_id, type(exc).__name__)
        return {"supported": False, "reason": "Relay could not verify the saved messages. Refresh or relink this session.", "items": []}
    return {"supported": True, "reason": None, "items": items, "state_at": fresh["state_at"],
            **_active_turn(rt, fresh, source, items)}


@router.post("/api/sessions/{session_id}/approval", status_code=202)
async def post_approval(
    request: Request,
    session_id: str,
    body: ApprovalBody,
    _: Any = Depends(require_user),
) -> dict[str, Any]:
    rt: Runtime = get_rt(request)
    if body.decision not in ("allow", "deny"):
        raise ApiError(400, "invalid_request", "decision must be 'allow' or 'deny'")
    await claude.answer_permission(rt, session_id, body.request_id, body.decision)
    return {"answered": True}
