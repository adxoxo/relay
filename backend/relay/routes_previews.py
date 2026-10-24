"""HTTP routes for previews and browser checks (section 6 of ARCHITECTURE.md).

Every route requires an authenticated user. The artifact route validates the
file name, resolves the path, and confirms it stays inside the run folder
before it serves the file with ``Cache-Control: no-store``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import checks, preview_discovery, preview_gateway, previews
from .errors import ApiError
from .runtime import Runtime, get_rt, require_user

router = APIRouter()

ARTIFACT_NAME_RE = re.compile(r"^[a-z0-9_-]{1,40}\.(png|json|txt)$")
MEDIA_TYPES = {".png": "image/png", ".json": "application/json", ".txt": "text/plain; charset=utf-8"}


# -- request models ----------------------------------------------------------

class PreviewCreate(BaseModel):
    env: str
    name: str
    command: str
    port: int
    project: str | None = ""
    health_path: str = "/"
    test_command: str | None = None


class PreviewUpdate(BaseModel):
    name: str | None = None
    command: str | None = None
    port: int | None = None
    project: str | None = None
    health_path: str | None = None
    test_command: str | None = None

    def as_fields(self) -> dict[str, Any]:
        return {k: v for k, v in self.model_dump(exclude_unset=True).items()}


class CheckRequest(BaseModel):
    viewports: list[str] | None = None


class AppChoiceKey(BaseModel):
    env: str = Field(max_length=40)
    project_dir: str = Field(max_length=4096)


class AppChoice(AppChoiceKey):
    candidate: str = Field(max_length=400)


# -- previews ----------------------------------------------------------------

async def phone_view(rt: Runtime, env_id: str, host_port: int | None, *, force: bool = False) -> dict[str, Any]:
    """Phone access for one endpoint. A URL appears only after a fresh route proof."""
    target = f"127.0.0.1:{host_port}" if host_port else None
    base: dict[str, Any] = {"url": None, "gateway_port": None, "target": target, "checked_at": None, "message": None}
    if host_port is None:
        return {**base, "status": "blocked"}
    gw = preview_gateway.gateway_for(rt.settings, env_id, host_port)
    mgr = preview_gateway.manager(rt)
    if gw is None or mgr is None or not gw.public_origin:
        return {**base, "status": "not_set_up", "gateway_port": gw.listen_port if gw else None}
    proof = await mgr.verify(gw, force=force)
    if preview_gateway.fresh(proof):
        return {**base, "status": "verified", "url": proof["url"], "gateway_port": gw.listen_port, "checked_at": proof["checked_at"]}
    return {
        **base,
        "status": "failed",
        "gateway_port": gw.listen_port,
        "checked_at": proof.get("checked_at"),
        "message": proof.get("message"),
    }


async def _list_view(rt: Runtime, *, force: bool = False) -> dict[str, Any]:
    rows = previews.list_previews_rows(rt.db)
    keyed = []
    registered: dict[str, dict[str, Any]] = {}
    for row in rows:
        _, host_port = await previews.row_host_port(rt, row)
        key = previews.endpoint_key(row["env"], int(row["port"]), host_port)
        keyed.append((row, key, host_port))
        registered[key] = {"env": row["env"], "host_port": host_port}
    scan = await preview_discovery.scan(rt, registered, force=force)
    by_key = {e["key"]: e for e in scan["endpoints"]}
    out = []
    claimed: set[str] = set()
    for row, key, host_port in keyed:
        last = checks.latest_run(rt.db, row["id"])
        last_view = checks.check_run_view(rt, last) if last is not None else None
        env_scan = scan["envs"].get(row["env"], {})
        listen_ports = env_scan.get("listen_ports") if env_scan.get("status") in ("ok", "partial") else None
        detected = None if key in claimed else by_key.get(key)
        claimed.add(key)
        out.append(
            await previews.preview_view(
                rt,
                row,
                last_check=last_view,
                detected=detected,
                listen_ports=listen_ports,
                phone=await phone_view(rt, row["env"], host_port, force=force),
            )
        )
    detected_only = []
    for endpoint in scan["endpoints"]:
        if endpoint["key"] in claimed:
            continue
        detected_only.append({**endpoint, "phone": await phone_view(rt, endpoint["env"], endpoint["host_port"], force=force)})
    return {
        "previews": out,
        "detected": detected_only,
        "app_choices": previews.app_groups(rt, out, detected_only),
        "scan": {
            "at": scan["at"],
            "envs": {k: {"status": v["status"], "message": v["message"]} for k, v in scan["envs"].items()},
            "docker": scan["docker"],
        },
    }


@router.get("/api/previews")
async def list_previews(request: Request, rt: Runtime = Depends(get_rt), _: Any = Depends(require_user)) -> JSONResponse:
    return JSONResponse(await _list_view(rt))


@router.post("/api/previews/scan")
async def rescan_previews(rt: Runtime = Depends(get_rt), _: Any = Depends(require_user)) -> JSONResponse:
    return JSONResponse(await _list_view(rt, force=True))


# Static app-choice routes come before the dynamic /api/previews/{preview_id}
# routes, so "app-choice" never reads as a preview id.

@router.put("/api/previews/app-choice")
async def put_app_choice(body: AppChoice, rt: Runtime = Depends(get_rt), _: Any = Depends(require_user)) -> JSONResponse:
    view = await _list_view(rt)
    previews.save_app_choice(rt, view["previews"], view["detected"], body.env, body.project_dir, body.candidate)
    view["app_choices"] = previews.app_groups(rt, view["previews"], view["detected"])
    return JSONResponse(view)


@router.delete("/api/previews/app-choice")
async def delete_app_choice(body: AppChoiceKey, rt: Runtime = Depends(get_rt), _: Any = Depends(require_user)) -> JSONResponse:
    previews.delete_app_choice(rt, body.env, body.project_dir)
    return JSONResponse(await _list_view(rt))


@router.post("/api/previews", status_code=201)
async def create_preview(
    body: PreviewCreate, rt: Runtime = Depends(get_rt), _: Any = Depends(require_user)
) -> JSONResponse:
    row = await previews.create_preview(rt, body.model_dump())
    view = await previews.preview_view(rt, row)
    return JSONResponse(view, status_code=201)


@router.patch("/api/previews/{preview_id}")
async def patch_preview(
    preview_id: str, body: PreviewUpdate, rt: Runtime = Depends(get_rt), _: Any = Depends(require_user)
) -> JSONResponse:
    row = await previews.update_preview(rt, preview_id, body.as_fields())
    view = await previews.preview_view(rt, row)
    return JSONResponse(view)


@router.delete("/api/previews/{preview_id}", status_code=200)
async def delete_preview(
    preview_id: str, rt: Runtime = Depends(get_rt), _: Any = Depends(require_user)
) -> JSONResponse:
    await previews.delete_preview(rt, preview_id)
    return JSONResponse({"deleted": True})


@router.post("/api/previews/{preview_id}/start", status_code=202)
async def start_preview(
    preview_id: str, rt: Runtime = Depends(get_rt), _: Any = Depends(require_user)
) -> JSONResponse:
    row = await previews.start_preview(rt, preview_id)
    view = await previews.preview_view(rt, row)
    return JSONResponse(view, status_code=202)


@router.post("/api/previews/{preview_id}/stop", status_code=200)
async def stop_preview(
    preview_id: str, rt: Runtime = Depends(get_rt), _: Any = Depends(require_user)
) -> JSONResponse:
    row = await previews.stop_preview(rt, preview_id)
    view = await previews.preview_view(rt, row)
    return JSONResponse(view)


@router.get("/api/previews/{preview_id}/logs")
async def preview_logs(
    preview_id: str, lines: int = 200, rt: Runtime = Depends(get_rt), _: Any = Depends(require_user)
) -> JSONResponse:
    text = await previews.preview_logs(rt, preview_id, lines)
    return JSONResponse({"text": text})


@router.post("/api/previews/{preview_id}/check", status_code=202)
async def run_check(
    preview_id: str,
    body: CheckRequest | None = None,
    rt: Runtime = Depends(get_rt),
    _: Any = Depends(require_user),
) -> JSONResponse:
    viewports = body.viewports if body is not None else None
    row = await checks.start_smoke_check(rt, preview_id, viewports)
    return JSONResponse(checks.check_run_view(rt, row), status_code=202)


@router.post("/api/previews/{preview_id}/test", status_code=202)
async def run_test(
    preview_id: str, rt: Runtime = Depends(get_rt), _: Any = Depends(require_user)
) -> JSONResponse:
    row = await checks.start_test_command(rt, preview_id)
    return JSONResponse(checks.check_run_view(rt, row), status_code=202)


# -- checks ------------------------------------------------------------------

@router.get("/api/checks")
async def list_checks(
    preview_id: str | None = None, limit: int = 20, rt: Runtime = Depends(get_rt), _: Any = Depends(require_user)
) -> JSONResponse:
    rows = checks.list_runs(rt.db, preview_id=preview_id, limit=limit)
    return JSONResponse({"runs": [checks.check_run_view(rt, r) for r in rows]})


@router.get("/api/checks/{run_id}")
async def get_check(
    run_id: str, rt: Runtime = Depends(get_rt), _: Any = Depends(require_user)
) -> JSONResponse:
    row = checks.get_run(rt.db, run_id)
    return JSONResponse(checks.check_run_view(rt, row))


@router.get("/api/checks/{run_id}/artifacts/{name}")
async def get_artifact(
    run_id: str, name: str, rt: Runtime = Depends(get_rt), _: Any = Depends(require_user)
) -> FileResponse:
    if not ARTIFACT_NAME_RE.match(name):
        raise ApiError(400, "invalid_request", "The artifact name is not valid.")
    checks.get_run(rt.db, run_id)  # 404 when the run does not exist
    run_folder = (rt.settings.artifacts_dir / run_id).resolve()
    target = (run_folder / name).resolve()
    if run_folder not in target.parents and target.parent != run_folder:
        raise ApiError(400, "invalid_request", "The artifact path is not valid.")
    if not target.is_file():
        raise ApiError(404, "not_found", "The artifact does not exist.")
    media_type = MEDIA_TYPES.get(Path(name).suffix, "application/octet-stream")
    return FileResponse(str(target), media_type=media_type, headers={"Cache-Control": "no-store"})
