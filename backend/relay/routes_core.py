"""Core HTTP routes and the change-notification WebSocket.

Public routes: ``/api/health`` and ``/api/auth/login``. Everything else needs
a valid credential. The ``/api/stream`` socket forwards coalesced bus topics
so a browser knows when to refetch a REST resource.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

from fastapi import APIRouter, Depends, Request, Response, WebSocket
from pydantic import BaseModel

from . import __version__
from .errors import ApiError
from .runtime import Principal, Runtime, get_rt, get_rt_ws, require_user

router = APIRouter()

COALESCE_INTERVAL = 0.15  # seconds to batch 'changed' topics
PING_INTERVAL = 25.0      # seconds between keepalive pings
WS_UNAUTHORIZED = 4401
WS_BAD_ORIGIN = 4403


class LoginBody(BaseModel):
    token: str


@router.get("/api/health")
async def health() -> dict:
    return {"ok": True, "version": __version__}


@router.post("/api/auth/login", status_code=204)
async def login(body: LoginBody, request: Request, response: Response) -> Response:
    rt: Runtime = get_rt(request)
    rt.auth.check_rate_limit(request)
    if not rt.auth.verify_token(body.token):
        rt.auth.record_failure(request)
        raise ApiError(401, "invalid_token", "The token is not valid.")
    rt.auth.clear_failures(request)
    cookie_value = rt.auth.create_session(request.headers.get("user-agent"))
    params = rt.auth.cookie_params(request)
    response.set_cookie(value=cookie_value, **params)
    from .auth import COOKIE_NAME, SECURE_COOKIE_NAME

    if params["key"] == SECURE_COOKIE_NAME:
        # Expire a plain cookie from before the __Host- prefix (it no longer works on https).
        response.delete_cookie(key=COOKIE_NAME, path="/", secure=True, httponly=True, samesite="strict")
    response.status_code = 204
    return response


@router.post("/api/auth/logout", status_code=204)
async def logout(request: Request, response: Response, user: Principal = Depends(require_user)) -> Response:
    rt: Runtime = get_rt(request)
    from .auth import COOKIE_NAME, SECURE_COOKIE_NAME

    cookie_value = rt.auth.session_cookie(request)
    if cookie_value:
        rt.auth.delete_session(cookie_value)
    params = rt.auth.clear_cookie_params(request)
    response.delete_cookie(**params)
    if params["key"] == SECURE_COOKIE_NAME:
        # A cookie from before the __Host- prefix can remain on a phone.
        response.delete_cookie(**{**params, "key": COOKIE_NAME})
    response.status_code = 204
    return response


@router.get("/api/auth/me")
async def me(user: Principal = Depends(require_user)) -> dict:
    return {
        "authenticated": True,
        "via": user.via,
        "tailscale_login": user.tailscale_login,
    }


@router.websocket("/api/stream")
async def stream(websocket: WebSocket) -> None:
    rt: Runtime = get_rt_ws(websocket)
    try:
        principal = await rt.auth.authenticate_websocket(websocket)
    except ApiError:
        await websocket.close(code=WS_BAD_ORIGIN)
        return
    if principal is None:
        await websocket.close(code=WS_UNAUTHORIZED)
        return

    await websocket.accept()
    send_lock = asyncio.Lock()

    async def send(obj: dict) -> None:
        async with send_lock:
            await websocket.send_json(obj)

    await send({"type": "hello", "version": __version__})

    with rt.bus.subscribe() as queue:
        pump = asyncio.create_task(_pump_topics(queue, send))
        receiver = asyncio.create_task(_answer_pings(websocket, send))
        try:
            # Either side ending (browser gone, send failure) ends the stream.
            await asyncio.wait({pump, receiver}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (pump, receiver):
                task.cancel()
            for task in (pump, receiver):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            with contextlib.suppress(Exception):
                await websocket.close()


async def _pump_topics(queue: "asyncio.Queue[str]", send) -> None:
    """Forward coalesced bus topics, and a keepalive ping when the bus is quiet."""
    pending: set[str] = set()
    last_ping = asyncio.get_running_loop().time()
    while True:
        timeout = COALESCE_INTERVAL if pending else PING_INTERVAL
        try:
            topic = await asyncio.wait_for(queue.get(), timeout=timeout)
            pending.add(topic)
            # Drain anything else already queued, then coalesce.
            await asyncio.sleep(COALESCE_INTERVAL)
            while not queue.empty():
                pending.add(queue.get_nowait())
            await send({"type": "changed", "topics": sorted(pending)})
            pending.clear()
        except asyncio.TimeoutError:
            if pending:
                await send({"type": "changed", "topics": sorted(pending)})
                pending.clear()
            now = asyncio.get_running_loop().time()
            if now - last_ping >= PING_INTERVAL:
                await send({"type": "ping"})
                last_ping = now


async def _answer_pings(websocket: WebSocket, send) -> None:
    """Read browser frames. A ``ping`` gets a ``pong``; a disconnect ends the loop.

    The browser cannot see protocol-level pings, so it sends its own heartbeat
    and reconnects when a pong does not arrive in time.
    """
    while True:
        try:
            message = await websocket.receive()
        except Exception:  # noqa: BLE001 - a closed socket ends the stream
            return
        if message.get("type") == "websocket.disconnect":
            return
        text = message.get("text")
        if not text:
            continue
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("type") == "ping":
            await send({"type": "pong"})
