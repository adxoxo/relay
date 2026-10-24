"""HTTP routes for the Files page.

Every route needs ``require_user``. A save is a ``PUT`` with cookie auth, so
it also needs an allowed ``Origin`` (``Auth.authenticate_request``). The body
of a save is the raw UTF-8 text, capped at ``files_edit_max_bytes`` before
relay reads it into memory.

The raw route serves images and videos inline only for an allowlisted media
type. Every other file (SVG, HTML, XML, PDF, unknown) is an attachment with
``application/octet-stream``. Both carry ``nosniff``, ``no-store``, and a
sandbox CSP, so no file content can run as a page in the relay origin.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import files
from .config import EnvConfig
from .errors import ApiError
from .runtime import Runtime, get_rt, require_user

router = APIRouter()

RAW_CSP = "sandbox; default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'"


def _env(rt: Runtime, env: str) -> EnvConfig:
    if env not in rt.settings.envs:
        raise ApiError(404, "unknown_env", f"Unknown environment: {env}")
    return rt.settings.env(env)


@router.get("/api/files/{env}/list")
async def list_folder(
    env: str,
    path: str = "",
    hidden: bool = False,
    rt: Runtime = Depends(get_rt),
    _: Any = Depends(require_user),
) -> dict:
    cfg = _env(rt, env)
    return await asyncio.to_thread(files.list_dir, cfg, path, hidden=hidden)


@router.get("/api/files/{env}/search")
async def search_names(
    env: str,
    q: str = Query(min_length=1, max_length=200),
    hidden: bool = False,
    rt: Runtime = Depends(get_rt),
    _: Any = Depends(require_user),
) -> dict:
    cfg = _env(rt, env)
    if "/" in q or "\x00" in q:
        raise ApiError(400, "invalid_query", "Search looks at file names only. Leave out '/'.")
    return await asyncio.to_thread(files.search, cfg, q, hidden=hidden)


@router.get("/api/files/{env}/meta")
async def file_meta(env: str, path: str, rt: Runtime = Depends(get_rt), _: Any = Depends(require_user)) -> dict:
    cfg = _env(rt, env)
    return await asyncio.to_thread(files.meta, cfg, rt.settings, path)


@router.get("/api/files/{env}/text")
async def read_text(env: str, path: str, rt: Runtime = Depends(get_rt), _: Any = Depends(require_user)) -> dict:
    cfg = _env(rt, env)
    return await asyncio.to_thread(files.read_text, cfg, rt.settings, path)


@router.put("/api/files/{env}/text")
async def save_text(
    env: str,
    path: str,
    request: Request,
    base_revision: str = Query(min_length=1, max_length=64),
    rt: Runtime = Depends(get_rt),
    _: Any = Depends(require_user),
) -> dict:
    cfg = _env(rt, env)
    limit = rt.settings.files_edit_max_bytes
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise ApiError(413, "too_large", f"relay saves text up to {limit:,} bytes.")
        except ValueError:
            raise ApiError(400, "invalid_request", "The Content-Length header is not valid.") from None
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise ApiError(413, "too_large", f"relay saves text up to {limit:,} bytes.")
    # One save per file at a time. The revision check and the rename run under the lock.
    files.split_path(path)
    async with rt.lock_for(f"file:{env}:{path}"):
        result = await asyncio.to_thread(files.save_text, cfg, rt.settings, path, bytes(body), base_revision)
    return result


@router.get("/api/files/{env}/page")
async def read_page(
    env: str,
    path: str,
    page: int = Query(0, ge=0, le=1_000_000),
    revision: str | None = Query(None, max_length=64),
    rt: Runtime = Depends(get_rt),
    _: Any = Depends(require_user),
) -> dict:
    cfg = _env(rt, env)
    return await asyncio.to_thread(files.read_page, cfg, rt.settings, path, page, revision)


@router.api_route("/api/files/{env}/raw", methods=["GET", "HEAD"])
async def raw_file(
    env: str,
    path: str,
    request: Request,
    download: bool = False,
    v: str | None = Query(None, max_length=64),
    rt: Runtime = Depends(get_rt),
    _: Any = Depends(require_user),
) -> Response:
    cfg = _env(rt, env)
    f = await asyncio.to_thread(files.open_file, cfg, path)
    try:
        # The page pins a media URL to one revision (?v=). A player that reads
        # in parts must never get bytes from two versions of the file.
        if v is not None and v != files.revision(f.st):
            raise ApiError(412, "revision_changed", "The file changed. Reload it.", revision=files.revision(f.st))
        size = f.st.st_size
        info = files.kind_by_name(f.name)
        mime = files.IMAGE_MIME.get(files._ext(f.name)) or files.VIDEO_MIME.get(files._ext(f.name))
        inline = not download and mime in files.INLINE_MIME and info["kind"] in ("image", "video")
        etag = f'"{files.revision(f.st)}"'
        headers = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": RAW_CSP,
            "Cross-Origin-Resource-Policy": "same-origin",
            "ETag": etag,
        }
        disposition = "inline" if inline else "attachment"
        headers["Content-Disposition"] = f"{disposition}; filename*=UTF-8''{quote(f.name, safe='')}"
        media_type = mime if inline else "application/octet-stream"
        wanted = files.parse_range(request.headers.get("range"), size)
        if_range = request.headers.get("if-range")
        if wanted is not None and if_range is not None and if_range.strip() != etag:
            wanted = None
        if wanted == "invalid":
            f.close()
            return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
        if isinstance(wanted, tuple):
            start, end = wanted
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        else:
            start, end = 0, size - 1
            status = 200
        headers["Content-Length"] = str(max(end - start + 1, 0))
        if request.method == "HEAD" or size == 0:
            f.close()
            return Response(status_code=status, headers=headers, media_type=media_type)
    except BaseException:
        f.close()
        raise

    async def body():
        it = files.iter_bytes(f.fd, start, end)
        try:
            while True:
                chunk = await asyncio.to_thread(next, it, None)
                if chunk is None:
                    break
                yield chunk
        finally:
            f.close()

    return StreamingResponse(body(), status_code=status, headers=headers, media_type=media_type)
