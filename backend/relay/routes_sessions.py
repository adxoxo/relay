"""HTTP routes for sessions, state, and projects.

Every route is protected by ``require_user``. Failures raise ``ApiError``,
which the app turns into ``{"error": code, "message": text}``. State-changing
routes publish on the bus so connected browsers refetch.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import sessions
from .errors import ApiError
from .runtime import Principal, Runtime, get_rt, require_user

router = APIRouter()


class CreateSessionBody(BaseModel):
    env: str
    kind: Literal["claude", "codex", "shell"]
    name: str | None = None
    project: str | None = None
    prompt: str | None = None
    permission_mode: Literal["manual", "acceptEdits", "auto", "plan"] | None = None


class EndSessionBody(BaseModel):
    tmux_session_id: str
    tmux_created: int
    pane_count: int = Field(ge=0)


class PromptBody(BaseModel):
    text: str


@router.get("/api/state")
async def get_state(request: Request, _user: Principal = Depends(require_user)) -> dict:
    rt: Runtime = get_rt(request)
    return sessions.state_view(rt)


@router.get("/api/sessions/{session_id}")
async def get_session(session_id: str, request: Request, _user: Principal = Depends(require_user)) -> dict:
    rt: Runtime = get_rt(request)
    row = sessions.get_row_or_404(rt, session_id)
    return sessions.session_view(rt, row)


@router.post("/api/sessions")
async def post_session(body: CreateSessionBody, request: Request, _user: Principal = Depends(require_user)) -> JSONResponse:
    rt: Runtime = get_rt(request)
    view = await sessions.create_session(
        rt,
        env_id=body.env,
        kind=body.kind,
        name=body.name,
        project=body.project,
        prompt=body.prompt,
        permission_mode=body.permission_mode,
    )
    rt.bus.publish("sessions")
    return JSONResponse(view, status_code=201)


@router.post("/api/sessions/{session_id}/end")
async def post_end(session_id: str, body: EndSessionBody, request: Request, _user: Principal = Depends(require_user)) -> dict:
    rt: Runtime = get_rt(request)
    result = await sessions.end_session(
        rt,
        session_id,
        tmux_session_id=body.tmux_session_id,
        tmux_created=body.tmux_created,
        pane_count=body.pane_count,
    )
    rt.bus.publish("sessions")
    return result


@router.post("/api/sessions/{session_id}/prompt")
async def post_prompt(session_id: str, body: PromptBody, request: Request, _user: Principal = Depends(require_user)) -> JSONResponse:
    rt: Runtime = get_rt(request)
    result = await sessions.send_prompt(rt, session_id, body.text)
    rt.bus.publish("sessions")
    return JSONResponse(result, status_code=202)


@router.post("/api/sessions/{session_id}/interrupt")
async def post_interrupt(session_id: str, request: Request, _user: Principal = Depends(require_user)) -> JSONResponse:
    rt: Runtime = get_rt(request)
    result = await sessions.interrupt(rt, session_id)
    rt.bus.publish("sessions")
    return JSONResponse(result, status_code=202)


@router.post("/api/sessions/{session_id}/resume")
async def post_resume(session_id: str, request: Request, _user: Principal = Depends(require_user)) -> JSONResponse:
    rt: Runtime = get_rt(request)
    view = await sessions.resume_session(rt, session_id)
    rt.bus.publish("sessions")
    return JSONResponse(view, status_code=201)


@router.get("/api/envs/{env}/projects")
async def get_projects(env: str, request: Request, _user: Principal = Depends(require_user)) -> dict:
    rt: Runtime = get_rt(request)
    if env not in rt.settings.envs:
        raise ApiError(404, "unknown_env", f"Unknown environment: {env}")
    return await sessions.list_projects(rt, env)
