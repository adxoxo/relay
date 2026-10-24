"""FastAPI application factory.

``create_app`` wires the runtime, authentication, security headers, error
handlers, the core and terminal routers, and (when ``features`` is true) the
feature routers and background loops from the other builders. It also serves
the built single-page app.

Run in production with::

    uvicorn relay.app:create_app --factory
"""

from __future__ import annotations

import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.gzip import GZipMiddleware

from . import __version__, routes_core, terminal
from .auth import Auth, origin_host
from .config import Settings, load_settings
from .errors import ApiError
from .runtime import Runtime, build_runtime

log = logging.getLogger("relay.app")

# Optional feature routers (other builders). Missing modules must not break us.
FEATURE_ROUTERS = ("routes_sessions", "routes_activity", "routes_previews", "routes_files")

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; connect-src 'self'; font-src 'self' data:; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    # A preview app on another port of the same host is same-site. It must not
    # embed relay responses (file media, JSON) in its own page.
    "Cross-Origin-Resource-Policy": "same-origin",
}


def _allowed_hosts(settings: Settings) -> set[str]:
    """Host header values accepted (hosts of allowed origins plus tailscale hostname)."""
    hosts: set[str] = set()
    for origin in settings.allowed_origins:
        host = origin_host(origin)
        if host:
            hosts.add(host)
            hosts.add(host.split(":")[0])
    if settings.tailscale_hostname:
        hosts.add(settings.tailscale_hostname)
    return hosts


def create_app(
    settings: Settings | None = None,
    *,
    runtime: Runtime | None = None,
    features: bool = True,
    background: bool = True,
) -> FastAPI:
    """Build the app.

    ``features`` adds the feature routers. ``background`` (with ``features``)
    runs the startup steps and loops that touch the containers: provisioning,
    inventory reconcile, the inbox poll, preview reconcile, and terminal client
    cleanup. A test harness turns ``background`` off to serve the feature
    routes without those side effects. Configured preview gateways start in
    both cases.
    """
    if runtime is not None:
        rt = runtime
        settings = rt.settings
    else:
        settings = settings or load_settings()
        rt = build_runtime(settings)
    rt.auth = Auth(settings, rt.db)
    allowed_hosts = _allowed_hosts(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import asyncio

        rt.bus.bind_loop(asyncio.get_running_loop())
        rt.auth.ensure_token()
        await _run_optional(rt, "preview_gateway", "start")
        if features and background:
            with contextlib.suppress(Exception):
                await terminal.cleanup_leftover_clients(rt)
            await _run_optional(rt, "provision", "provision_all")
            await _run_optional(rt, "inventory", "startup_reconcile")
            await _run_optional(rt, "checks", "reconcile_startup")
            _start_background(rt, "inventory", "run_forever")
            _start_background(rt, "inbox", "run_forever")
            _start_background(rt, "previews", "run_forever")
        try:
            yield
        finally:
            for task in rt.background:
                task.cancel()
            for task in rt.background:
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await task
            rt.background.clear()
            await _run_optional(rt, "preview_gateway", "stop")
            with contextlib.suppress(Exception):
                await terminal.close_all_bridges()
            with contextlib.suppress(Exception):
                rt.db.close()

    app = FastAPI(title="relay", version=__version__, lifespan=lifespan)
    app.state.rt = rt

    # -- error handlers ------------------------------------------------------
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.body())

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = []
        for err in exc.errors():
            loc = [str(part) for part in err.get("loc", [])]
            # ``field`` is the request field name (last string part after "body"/"query").
            field = next((p for p in reversed(loc) if p not in ("body", "query", "path") and not p.isdigit()), None)
            msg = err.get("msg", "")
            details.append({"loc": loc, "msg": msg, "field": field, "message": msg})
        return JSONResponse(
            status_code=422,
            content={"error": "invalid_request", "message": "Request validation failed.", "details": details},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"error": "internal_error", "message": "Internal server error."})

    # -- host check and security headers -------------------------------------
    @app.middleware("http")
    async def _guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if allowed_hosts and (not host or (host not in allowed_hosts and host.split(":")[0] not in allowed_hosts)):
            return JSONResponse(status_code=400, content={"error": "bad_host", "message": "Unknown host."})
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # -- routers -------------------------------------------------------------
    app.include_router(routes_core.router)
    app.include_router(terminal.router)
    if features:
        for name in FEATURE_ROUTERS:
            _include_optional_router(app, name)

    _install_spa(app, settings)
    return app


async def _run_optional(rt: Runtime, module_name: str, func_name: str) -> None:
    """Call ``module.func(rt)`` if resolvable, else warn and continue."""
    import asyncio

    func = _resolve(module_name, func_name)
    if func is None:
        log.warning("startup step %s.%s is unavailable; skipping", module_name, func_name)
        return
    try:
        result = func(rt)
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        log.exception("startup step %s.%s failed; continuing", module_name, func_name)


def _start_background(rt: Runtime, module_name: str, func_name: str) -> None:
    """Schedule ``module.func(rt)`` as a background task if resolvable."""
    import asyncio

    func = _resolve(module_name, func_name)
    if func is None:
        log.warning("background loop %s.%s is unavailable; skipping", module_name, func_name)
        return
    try:
        rt.background.append(asyncio.create_task(func(rt)))
    except Exception:
        log.exception("could not start background loop %s.%s", module_name, func_name)


def _resolve(module_name: str, func_name: str):
    import importlib

    try:
        module = importlib.import_module(f".{module_name}", package="relay")
    except Exception:
        return None
    return getattr(module, func_name, None)


def _include_optional_router(app: FastAPI, module_name: str) -> None:
    import importlib

    try:
        module = importlib.import_module(f".{module_name}", package="relay")
    except Exception as exc:
        log.warning("feature router %s is unavailable; skipping (%s)", module_name, exc)
        return
    router = getattr(module, "router", None)
    if router is None:
        log.warning("feature module %s has no router; skipping", module_name)
        return
    app.include_router(router)


def _install_spa(app: FastAPI, settings: Settings) -> None:
    dist: Path = settings.frontend_dist
    assets_dir = dist / "assets"
    index_file = dist / "index.html"

    if assets_dir.is_dir():
        app.mount(
            "/assets",
            _AssetGZip(_ImmutableStatic(directory=str(assets_dir))),
            name="assets",
        )

    @app.get("/{full_path:path}")
    async def spa(full_path: str, request: Request) -> Response:
        path = "/" + full_path
        if path.startswith("/api"):
            return JSONResponse(status_code=404, content={"error": "not_found", "message": "Unknown API path."})
        last = path.rsplit("/", 1)[-1]
        if "." in last:
            # A missing file (for example /favicon.ico) is a 404, not the app shell.
            return Response(status_code=404, headers={"Cache-Control": "no-store"})
        if index_file.is_file():
            return FileResponse(str(index_file), headers={"Cache-Control": "no-store"})
        return HTMLResponse(
            "<!doctype html><html><head><meta charset='utf-8'><title>relay</title></head>"
            "<body style='font-family:sans-serif;padding:2rem'>"
            "<h1>relay</h1><p>The frontend is not built. Run the frontend build to create "
            "<code>frontend/dist</code>.</p></body></html>",
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )


def _gzip_accepted(accept_encoding: str) -> bool:
    """True when Accept-Encoding names gzip with a quality above zero (RFC 9110 12.5.3)."""
    for part in accept_encoding.split(","):
        coding, _, params = part.strip().partition(";")
        if coding.strip().lower() not in ("gzip", "x-gzip"):
            continue
        quality = 1.0
        for param in params.split(";"):
            name, _, value = param.strip().partition("=")
            if name.strip().lower() == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        return quality > 0
    return False


class _AssetGZip:
    """gzip for the built assets under /assets only.

    The API, the app shell, WebSockets and raw project files never pass through
    here. Starlette's GZipMiddleware leaves partial (206) responses, bodies under
    ``minimum_size``, already encoded and binary media types unchanged, and adds
    ``Vary: Accept-Encoding``. It tests Accept-Encoding for the substring
    "gzip", so a refused gzip (``gzip;q=0``) is removed from the request first.

    A gzip body is a different representation, so its ETag becomes weak.
    If-None-Match still matches it (StaticFiles compares without W/), and a 304
    for a cached gzip copy (the client sends the weak tag) keeps the weak tag
    and ``Vary``. If-Range keeps the strong identity ETag.
    """

    def __init__(self, app) -> None:
        self._gzip = GZipMiddleware(app, minimum_size=1024, compresslevel=6)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self._gzip(scope, receive, send)
            return
        request = Headers(scope=scope)
        accept = request.get("accept-encoding", "")
        gzip_ok = _gzip_accepted(accept)
        if "gzip" in accept.lower() and not gzip_ok:
            raw = [(k, v) for k, v in scope["headers"] if k.lower() != b"accept-encoding"]
            scope = {**scope, "headers": [*raw, (b"accept-encoding", b"identity")]}
        if_none_match = [tag.strip() for tag in request.get("if-none-match", "").split(",")]

        async def send_with_validators(message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                etag = headers.get("etag")
                if etag and not etag.startswith("W/"):
                    if headers.get("content-encoding") == "gzip":
                        headers["etag"] = "W/" + etag
                    elif message["status"] == 304 and gzip_ok and "W/" + etag in if_none_match:
                        headers["etag"] = "W/" + etag
                        headers.add_vary_header("Accept-Encoding")
            await send(message)

        await self._gzip(scope, receive, send_with_validators)


class _ImmutableStatic(StaticFiles):
    """Serve built assets with a long immutable cache (hashed filenames)."""

    def is_not_modified(self, response_headers, request_headers) -> bool:  # type: ignore[override]
        return super().is_not_modified(response_headers, request_headers)

    async def get_response(self, path: str, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        with contextlib.suppress(Exception):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response
