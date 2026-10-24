"""Shared runtime state and FastAPI dependencies.

``Runtime`` is created once by the app factory and stored on
``app.state.rt``. Feature modules receive it instead of importing globals,
so tests can build a runtime with a temporary database and fake podman.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import Request, WebSocket

from .bus import Bus
from .config import Settings
from .db import Database
from .errors import ApiError
from .podman import ContainerInfo, Podman
from .tmux import Snapshot, Tmux

if TYPE_CHECKING:  # pragma: no cover
    pass


@dataclass
class Principal:
    """An authenticated caller."""

    via: str  # 'cookie' | 'bearer'
    auth_session_id: str | None = None
    tailscale_login: str | None = None


class Authenticator(Protocol):
    """Implemented by ``relay.auth.Auth`` and installed as ``rt.auth``."""

    async def authenticate_request(self, request: Request) -> Principal: ...

    async def authenticate_websocket(self, websocket: WebSocket) -> Principal | None: ...


@dataclass
class EnvStatus:
    """Latest known availability of one environment, maintained by the inventory loop."""

    container: ContainerInfo | None = None
    snapshot: Snapshot | None = None
    snapshot_at: float | None = None
    error: str | None = None
    provision: dict[str, Any] | None = None  # result of relay.provision.provision_env


@dataclass
class Runtime:
    settings: Settings
    db: Database
    bus: Bus
    podman: Podman
    tmux: dict[str, Tmux]
    env_status: dict[str, EnvStatus]
    auth: Any = None  # relay.auth.Auth, installed by the app factory
    started_at: float = field(default_factory=time.time)
    inventory_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    background: list[asyncio.Task[Any]] = field(default_factory=list)
    # Process-local caches of feature modules (preview scan, gateway manager).
    cache: dict[str, Any] = field(default_factory=dict)

    def lock_for(self, key: str) -> asyncio.Lock:
        """A lock per session (or preview) id, used to serialize operations on it."""
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    def tmux_for(self, env_id: str) -> Tmux:
        try:
            return self.tmux[env_id]
        except KeyError:
            raise ApiError(404, "unknown_env", f"Unknown environment: {env_id}") from None


def build_runtime(settings: Settings, *, podman: Podman | None = None, db: Database | None = None) -> Runtime:
    pm = podman or Podman(settings.podman_binary)
    return Runtime(
        settings=settings,
        db=db or Database(settings.db_path),
        bus=Bus(),
        podman=pm,
        tmux={env_id: Tmux(pm, env) for env_id, env in settings.envs.items()},
        env_status={env_id: EnvStatus() for env_id in settings.envs},
    )


# -- FastAPI dependencies ----------------------------------------------------

def get_rt(request: Request) -> Runtime:
    return request.app.state.rt


def get_rt_ws(websocket: WebSocket) -> Runtime:
    return websocket.app.state.rt


async def require_user(request: Request) -> Principal:
    """Dependency for every protected HTTP route. Raises 401/403 via ``ApiError``."""
    rt: Runtime = request.app.state.rt
    if rt.auth is None:
        raise ApiError(503, "auth_unavailable", "Authentication is not configured")
    return await rt.auth.authenticate_request(request)
