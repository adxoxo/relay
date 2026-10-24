"""Discovery: attribution, HTTP-only, Docker workspace links, merge, phone link, check target."""

from __future__ import annotations

import json
import time

import pytest

from fakes import FakePodman, ok
from relay import checks, preview_discovery, preview_gateway, previews, routes_previews
from relay.config import GatewayConfig
from relay.db import new_id, now
from relay.runtime import Runtime


def listener(port, protocol="http", cwd="/home/node/projects/garden", tool="vite"):
    return {"port": port, "binds": ["any"], "protocol": protocol, "pid": 4100 + port % 100, "comm": "node",
            "tool": tool, "cwd": cwd, "started_at": 1_790_000_000.0}


def scan_output(listeners, listen_ports=None):
    ports = listen_ports if listen_ports is not None else [x["port"] for x in listeners]
    return json.dumps({"listeners": listeners, "listen_ports": ports, "partial": False, "processes": 12})


def wire(fake: FakePodman, per_env: dict[str, str]) -> None:
    def handler(args, _input):
        if args[0] == "exec" and "python3" in args:
            container = args[args.index("python3") - 1]
            return ok(per_env.get(container, scan_output([])) + "\n")
        return ok()

    fake.handler = handler


def register(rt: Runtime, env: str, port: int, name: str) -> str:
    pid = new_id()
    rt.db.insert(
        "previews",
        {"id": pid, "env": env, "name": name, "project_dir": "/home/node/projects/garden", "command": "npm run dev",
         "port": port, "health_path": "/", "test_command": None, "created_at": now(), "updated_at": now()},
    )
    return pid


@pytest.fixture(autouse=True)
def no_docker(rt):
    object.__setattr__(rt.settings, "docker_socket", None)


async def test_only_http_listeners_owned_in_the_container_become_endpoints(rt: Runtime, fake_podman: FakePodman):
    wire(
        fake_podman,
        {
            "job-work2": scan_output(
                [listener(3000), listener(5432, protocol=None, tool=None), listener(8765), listener(8888, cwd="/home/node")]
            ),
            "job-work1": scan_output([listener(5173), listener(5174)]),
        },
    )
    scan = await preview_discovery.scan(rt, {})
    keys = {e["key"]: e for e in scan["endpoints"]}
    assert set(keys) == {"work2:3000", "work2:8888", "work1:15173", "work1:c5174"}
    assert keys["work2:3000"]["project"] == "garden" and keys["work2:3000"]["process"]["tool"] == "vite"
    assert keys["work2:8888"]["project"] is None and keys["work2:8888"]["folder"] == "/home/node"
    assert keys["work1:15173"]["host_port"] == 15173 and keys["work1:15173"]["reachable"] is True
    assert keys["work1:c5174"]["reachable"] is False and keys["work1:c5174"]["network"] == "unpublished"


async def test_one_card_per_endpoint_and_registered_controls_stay(rt: Runtime, fake_podman: FakePodman):
    wire(
        fake_podman,
        {"job-work2": scan_output([listener(3000), listener(8888)]), "job-work1": scan_output([listener(5174)])},
    )
    pid = register(rt, "work2", 3000, "garden web")
    view = await routes_previews._list_view(rt)
    assert [p["id"] for p in view["previews"]] == [pid]
    card = view["previews"][0]
    assert card["detected"]["key"] == "work2:3000" and card["source"] == "detected"
    detected = {d["key"]: d for d in view["detected"]}
    assert set(detected) == {"work2:8888", "work1:c5174"}  # the registered endpoint is not listed twice
    assert detected["work1:c5174"]["phone"]["status"] == "blocked"
    assert detected["work2:8888"]["phone"]["status"] == "not_set_up"
    assert card["urls"]["tailnet"] is None


async def test_scan_errors_are_reported_per_source(rt: Runtime, fake_podman: FakePodman):
    def handler(args, _input):
        from relay.podman import ProcResult

        return ProcResult(1, b"", b"boom")

    fake_podman.handler = handler
    view = await routes_previews._list_view(rt)
    assert view["scan"]["envs"]["work2"]["status"] == "error"
    assert view["detected"] == []


def test_docker_needs_a_verified_host_path(rt: Runtime):
    w2_projects = rt.settings.env("work2").host_home / "projects"
    (w2_projects / "notes-app").mkdir(parents=True)
    services = rt.settings.env("work2").host_home.parent / "services"
    services.mkdir()
    compose = {"Labels": {"com.docker.compose.project.working_dir": str(w2_projects / "notes-app")}, "Mounts": []}
    container_path = {"Labels": {}, "Mounts": [{"Type": "bind", "Source": "/home/node/projects/notes-app"}]}
    elsewhere = {"Labels": {"com.docker.compose.project.working_dir": str(services)}, "Mounts": []}
    assert preview_discovery.docker_link(rt.settings, compose) == {"env": "work2", "project": "notes-app", "how": "compose"}
    assert preview_discovery.docker_link(rt.settings, container_path) is None
    assert preview_discovery.docker_link(rt.settings, elsewhere) is None


async def test_docker_endpoint_joins_by_link_or_registration(rt: Runtime, fake_podman: FakePodman, monkeypatch, tmp_path):
    wire(fake_podman, {})
    sock = tmp_path / "docker.sock"
    sock.write_text("")
    object.__setattr__(rt.settings, "docker_socket", str(sock))
    w2_projects = rt.settings.env("work2").host_home / "projects"
    (w2_projects / "notes-app").mkdir(parents=True)

    def c(name, port, labels=None, mounts=None):
        return {"Names": [f"/{name}"], "Image": "nginx", "State": "running", "Created": 1,
                "Labels": labels or {}, "Mounts": mounts or [],
                "Ports": [{"IP": "0.0.0.0", "PrivatePort": 80, "PublicPort": port, "Type": "tcp"}]}

    containers = [
        c("notes-web", 8081, labels={"com.docker.compose.project.working_dir": str(w2_projects / "notes-app")}),
        c("ambiguous", 8000, mounts=[{"Type": "bind", "Source": "/home/node/projects/x"}]),
        c("registered", 8090, mounts=[{"Type": "bind", "Source": "/home/node/projects/y"}]),
        c("db", 8091, labels={"com.docker.compose.project.working_dir": str(w2_projects / "notes-app")}),
    ]

    async def fake_containers(_path):
        return containers

    async def fake_probe(port):
        return None if port == 8091 else "http"

    monkeypatch.setattr(preview_discovery, "docker_containers", fake_containers)
    monkeypatch.setattr(preview_discovery, "http_probe", fake_probe)
    pid = register(rt, "work2", 8090, "registered app")
    view = await routes_previews._list_view(rt)
    detected = {d["key"]: d for d in view["detected"]}
    assert set(detected) == {"work2:8081"}
    assert detected["work2:8081"]["docker"]["link"] == "compose"
    card = next(p for p in view["previews"] if p["id"] == pid)
    assert card["detected"]["docker"]["name"] == "registered"


async def test_phone_link_needs_a_fresh_route_proof(rt: Runtime, fake_podman: FakePodman, monkeypatch):
    wire(fake_podman, {"job-work2": scan_output([listener(3000)])})
    gw = GatewayConfig(listen_port=18501, env="work2", target_port=3000, public_origin="https://laptop.example.ts.net:18501")
    object.__setattr__(rt.settings, "preview_gateways", (gw,))
    mgr = preview_gateway.GatewayManager(rt)
    rt.cache["gateways"] = mgr
    proof = {"status": "failed", "checked_at": time.time(), "message": "no route", "url": None, "gateway_port": 18501}

    async def fake_verify(_gw, force=False):
        mgr.proofs[18501] = dict(proof)
        return mgr.proofs[18501]

    monkeypatch.setattr(mgr, "verify", fake_verify)
    pid = register(rt, "work2", 3000, "garden web")
    view = await routes_previews._list_view(rt)
    card = view["previews"][0]
    assert card["phone"]["status"] == "failed" and card["phone"]["url"] is None and card["urls"]["tailnet"] is None
    assert await checks._target(rt, previews.get_preview(rt.db, pid)) == ("http://127.0.0.1:3000", False)

    proof.update(status="verified", url="https://laptop.example.ts.net:18501/", message=None)
    view = await routes_previews._list_view(rt, force=True)
    card = view["previews"][0]
    assert card["phone"]["status"] == "verified"
    assert card["urls"]["tailnet"] == "https://laptop.example.ts.net:18501/"
    assert await checks._target(rt, previews.get_preview(rt.db, pid)) == ("https://laptop.example.ts.net:18501/", True)

    # A stale proof is not enough: relay proves the route again before it uses it.
    proof.update(status="failed", url=None)
    mgr.proofs[18501]["checked_at"] = time.time() - 3600
    assert await checks._target(rt, previews.get_preview(rt.db, pid)) == ("http://127.0.0.1:3000", False)


async def test_check_target_has_no_url_for_an_unpublished_port(rt: Runtime, fake_podman: FakePodman):
    pid = register(rt, "work1", 5174, "not published")
    assert await checks._target(rt, previews.get_preview(rt.db, pid)) == (None, False)
