"""App choices on the Previews page: groups, suggestion, saved choice, identity, routes."""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from fakes import FakePodman, ok
from relay import previews, routes_previews
from relay.app import create_app
from relay.auth import Auth
from relay.db import Database, new_id, now
from relay.runtime import Runtime

ROOT = "/home/node/projects"
BASE = "http://127.0.0.1:8765"


def listener(port, cwd, tool="vite", pid=None, started_at=1_790_000_000.0):
    return {"port": port, "binds": ["any"], "protocol": "http", "pid": pid or 4000 + port % 1000, "comm": "node",
            "tool": tool, "cwd": cwd, "started_at": started_at}


def wire(fake: FakePodman, work2: list[dict]) -> None:
    out = json.dumps({"listeners": work2, "listen_ports": [x["port"] for x in work2], "partial": False, "processes": 9})

    def handler(args, _input):
        if args[0] == "exec" and "python3" in args:
            return ok((out if "job-work2" in args else json.dumps({"listeners": [], "listen_ports": []})) + "\n")
        return ok()

    fake.handler = handler


def repo(rt: Runtime, rel: str, *, worktree: bool = False) -> str:
    """Create a project folder with a .git marker on the host side of work2."""
    path = rt.settings.env("work2").host_home / "projects" / rel
    path.mkdir(parents=True, exist_ok=True)
    if worktree:
        (path / ".git").write_text("gitdir: /home/node/projects/garden/.git/worktrees/x\n")
    else:
        (path / ".git").mkdir(exist_ok=True)
        (path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return f"{ROOT}/{rel}"


def folder(rt: Runtime, rel: str) -> str:
    (rt.settings.env("work2").host_home / "projects" / rel).mkdir(parents=True, exist_ok=True)
    return f"{ROOT}/{rel}"


def register(rt: Runtime, port: int, project_dir: str, name: str = "garden web") -> str:
    pid = new_id()
    rt.db.insert(
        "previews",
        {"id": pid, "env": "work2", "name": name, "project_dir": project_dir, "command": "npm run dev",
         "port": port, "health_path": "/", "test_command": None, "created_at": now(), "updated_at": now()},
    )
    return pid


async def view(rt: Runtime) -> dict:
    rt.cache.pop("scan", None)
    return await routes_previews._list_view(rt)


def groups(v: dict) -> dict[str, dict]:
    return {g["project"]: g for g in v["app_choices"]}


async def choose(rt: Runtime, project_dir: str, candidate: str) -> None:
    v = await view(rt)
    previews.save_app_choice(rt, v["previews"], v["detected"], "work2", project_dir, candidate)


@pytest.fixture(autouse=True)
def no_docker(rt):
    object.__setattr__(rt.settings, "docker_socket", None)


async def test_groups_suggest_one_frontend_and_keep_unknown_servers_out(rt: Runtime, fake_podman: FakePodman):
    garden = repo(rt, "garden")
    board = repo(rt, "board")
    wt = repo(rt, "garden-wt", worktree=True)
    api_only = folder(rt, "api-only")
    wire(fake_podman, [
        listener(5173, f"{garden}/web"),                           # garden: the one frontend tool
        listener(8001, f"{garden}/api", tool="uvicorn"),           # garden: an API, not a UI
        listener(5174, f"{board}/app"),                            # board: two frontend tools
        listener(6006, f"{board}", tool="storybook"),
        listener(5175, wt),                                        # a worktree is its own repository
        {**listener(8000, f"{api_only}/srv", tool="http.server"), "served_dir": f"{api_only}/srv"},  # no .git, no index.html
        listener(8888, "/home/node"),                              # outside the projects root
        listener(8889, ROOT),                                      # the projects root itself
    ])
    g = groups(await view(rt))
    assert set(g) == {"garden", "board", "garden-wt", "api-only/srv"}
    assert g["garden"]["state"] == "suggested" and g["garden"]["app"] == "proc:work2:5173:4173"
    assert [c["tool"] for c in g["garden"]["candidates"]] == ["vite", "uvicorn"]  # frontend first
    assert g["board"]["state"] == "needs_choice" and g["board"]["app"] is None
    assert g["garden-wt"]["state"] == "suggested"
    assert g["api-only/srv"]["state"] == "needs_choice"  # http.server alone never becomes an app
    # The two servers without a verified project stay in the detected list only.
    in_groups = {c["endpoint_key"] for grp in g.values() for c in grp["candidates"]}
    assert {"work2:8888", "work2:8889"}.isdisjoint(in_groups)


async def test_symlinked_git_marker_does_not_make_a_group(rt: Runtime, fake_podman: FakePodman):
    repo(rt, "garden")
    linked = folder(rt, "linked")
    host = rt.settings.env("work2").host_home / "projects"
    os.symlink(host / "garden" / ".git", host / "linked" / ".git")
    wire(fake_podman, [listener(5173, f"{linked}/web")])
    g = groups(await view(rt))
    assert set(g) == {"linked/web"}  # fallback: the verified cwd


async def test_saved_choice_wins_over_the_suggestion_and_persists(rt: Runtime, fake_podman: FakePodman):
    garden = repo(rt, "garden")
    wire(fake_podman, [listener(5173, f"{garden}/web"), listener(8001, f"{garden}/api", tool="uvicorn")])
    await choose(rt, garden, "proc:work2:8001:4001")
    g = groups(await view(rt))["garden"]
    assert g["state"] == "saved" and g["app"] == "proc:work2:8001:4001" and g["saved"]["available"] is True
    assert g["saved"]["snapshot"] == {"tool": "uvicorn", "name": "node", "port": 8001, "host_port": 8001}
    # A new connection to the same file still has the choice.
    again = Database(rt.settings.db_path)
    row = again.one("SELECT * FROM preview_app_choices")
    again.close()
    assert (row["env"], row["project_dir"], row["kind"]) == ("work2", garden, "process")
    assert "http" not in row["identity_json"]  # no URL is stored


async def test_unknown_tool_needs_the_same_pid_and_a_restart_shows_stopped(rt: Runtime, fake_podman: FakePodman):
    garden = repo(rt, "garden")
    wire(fake_podman, [listener(9000, f"{garden}/web", tool=None, pid=4100)])
    await choose(rt, garden, "proc:work2:9000:4100")
    # Another process on the same port after a restart: not bound.
    wire(fake_podman, [listener(9000, f"{garden}/web", tool=None, pid=4200, started_at=1_790_000_500.0)])
    g = groups(await view(rt))["garden"]
    assert g["state"] == "saved" and g["app"] is None and g["saved"]["available"] is False
    assert g["saved"]["snapshot"]["port"] == 9000
    # The server stopped entirely: the saved card stays, still unbound.
    wire(fake_podman, [])
    g = groups(await view(rt))["garden"]
    assert g["state"] == "saved" and g["app"] is None and g["candidates"] == []


async def test_known_tool_rebinds_after_restart_but_not_to_another_tool_or_port(rt: Runtime, fake_podman: FakePodman):
    garden = repo(rt, "garden")
    wire(fake_podman, [listener(5173, f"{garden}/web", pid=4100)])
    await choose(rt, garden, "proc:work2:5173:4100")
    wire(fake_podman, [listener(5173, f"{garden}/web", pid=4300)])  # vite restarted
    assert groups(await view(rt))["garden"]["app"] == "proc:work2:5173:4300"
    # vite now runs on another port and next took the old port: relay keeps the choice unbound.
    wire(fake_podman, [listener(5174, f"{garden}/web", pid=4301), listener(5173, f"{garden}/web", tool="next", pid=4302)])
    g = groups(await view(rt))["garden"]
    assert g["state"] == "saved" and g["app"] is None


async def test_a_reused_port_in_another_project_never_binds(rt: Runtime, fake_podman: FakePodman):
    garden = repo(rt, "garden")
    board = repo(rt, "board")
    wire(fake_podman, [listener(5173, f"{garden}/web")])
    await choose(rt, garden, "proc:work2:5173:4173")
    wire(fake_podman, [listener(5173, f"{board}/web")])  # same port, same tool, another project
    g = groups(await view(rt))
    assert g["garden"]["state"] == "saved" and g["garden"]["app"] is None
    assert g["board"]["state"] == "suggested"
    v = await view(rt)
    with pytest.raises(Exception) as exc:
        previews.save_app_choice(rt, v["previews"], v["detected"], "work2", garden, "proc:work2:5173:4173")
    assert getattr(exc.value, "code", None) == "wrong_project"


async def test_registered_choice_follows_a_port_edit_but_not_a_project_move(rt: Runtime, fake_podman: FakePodman):
    garden = repo(rt, "garden")
    board = repo(rt, "board")
    wire(fake_podman, [listener(5173, f"{garden}/web"), listener(5174, f"{garden}/docs")])
    pid = register(rt, 4999, f"{garden}/web")
    g = groups(await view(rt))["garden"]
    reg = next(c for c in g["candidates"] if c["kind"] == "registered")
    assert reg["status"] == "stopped" and reg["preview_id"] == pid
    await choose(rt, garden, f"reg:{pid}")
    rt.db.update("previews", pid, {"port": 5173})  # the port edit: the registered row now owns 5173
    g = groups(await view(rt))["garden"]
    assert g["app"] == f"reg:{pid}"
    app = next(c for c in g["candidates"] if c["id"] == g["app"])
    assert app["status"] == "running" and app["tool"] == "vite" and app["endpoint_key"] == "work2:5173"
    rt.db.update("previews", pid, {"project_dir": f"{board}/web"})  # moved to another project
    g = groups(await view(rt))
    assert g["garden"]["state"] == "saved" and g["garden"]["app"] is None
    assert g["board"]["state"] == "suggested"  # the move never carries the choice
    previews.delete_app_choice(rt, "work2", garden)
    await choose(rt, board, f"reg:{pid}")
    rt.db.execute("DELETE FROM previews WHERE id = ?", (pid,))  # Remove cleans the choice
    assert rt.db.one("SELECT * FROM preview_app_choices") is None


async def test_a_detected_choice_survives_registration_of_the_same_server(rt: Runtime, fake_podman: FakePodman):
    garden = repo(rt, "garden")
    wire(fake_podman, [listener(8001, f"{garden}/api", tool="uvicorn")])
    await choose(rt, garden, "proc:work2:8001:4001")
    pid = register(rt, 8001, f"{garden}/api", name="garden api")
    g = groups(await view(rt))["garden"]
    assert g["app"] == f"reg:{pid}"


async def test_registered_parent_folder_groups_a_server_without_git(rt: Runtime, fake_podman: FakePodman):
    mono = folder(rt, "mono")
    folder(rt, "mono/apps/web")
    wire(fake_podman, [listener(5173, f"{mono}/apps/web")])
    register(rt, 4999, mono, name="mono")
    g = groups(await view(rt))
    assert set(g) == {"mono"} and len(g["mono"]["candidates"]) == 2


async def test_docker_choice_needs_the_same_container_name(rt: Runtime, fake_podman: FakePodman, monkeypatch, tmp_path):
    from relay import preview_discovery

    wire(fake_podman, [])
    sock = tmp_path / "docker.sock"
    sock.write_text("")
    object.__setattr__(rt.settings, "docker_socket", str(sock))
    notes = repo(rt, "notes-app")
    host = rt.settings.env("work2").host_home / "projects" / "notes-app"
    containers = [{"Names": ["/notes-web"], "Image": "nginx", "State": "running", "Created": 1,
                   "Labels": {"com.docker.compose.project.working_dir": str(host)}, "Mounts": [],
                   "Ports": [{"IP": "0.0.0.0", "PrivatePort": 80, "PublicPort": 8081, "Type": "tcp"}]}]

    async def fake_containers(_path):
        return containers

    async def fake_probe(_port):
        return "http"

    monkeypatch.setattr(preview_discovery, "docker_containers", fake_containers)
    monkeypatch.setattr(preview_discovery, "http_probe", fake_probe)
    g = groups(await view(rt))["notes-app"]
    assert g["state"] == "needs_choice" and g["candidates"][0]["kind"] == "docker"  # Docker is not a known UI
    await choose(rt, notes, "docker:work2:8081:notes-web")
    assert groups(await view(rt))["notes-app"]["app"] == "docker:work2:8081:notes-web"
    containers[0]["Names"] = ["/other-web"]
    assert groups(await view(rt))["notes-app"]["app"] is None


# -- routes -------------------------------------------------------------------

@pytest.fixture
def client(rt):
    app = create_app(runtime=rt, features=True, background=False)
    with TestClient(app, base_url=BASE) as c:
        yield c


def login(c: TestClient, rt: Runtime) -> None:
    token = Auth(rt.settings, rt.db).ensure_token()
    assert c.post("/api/auth/login", json={"token": token}).status_code == 204


def test_routes_need_auth_and_origin_and_accept_only_current_candidates(rt: Runtime, fake_podman: FakePodman, client):
    garden = repo(rt, "garden")
    wire(fake_podman, [listener(5173, f"{garden}/web"), listener(8001, f"{garden}/api", tool="uvicorn")])
    body = {"env": "work2", "project_dir": garden, "candidate": "proc:work2:8001:4001"}
    assert client.put("/api/previews/app-choice", json=body, headers={"Origin": BASE}).status_code == 401
    login(client, rt)
    assert client.put("/api/previews/app-choice", json=body, headers={"Origin": "http://127.0.0.1:18501"}).status_code == 403
    listed = client.get("/api/previews").json()
    assert listed["app_choices"][0]["state"] == "suggested"
    url = {**body, "candidate": "http://127.0.0.1:8001/"}
    res = client.put("/api/previews/app-choice", json=url, headers={"Origin": BASE})
    assert res.status_code == 409 and res.json()["error"] == "candidate_gone"
    outside = {**body, "project_dir": "/home/node/elsewhere"}
    assert client.put("/api/previews/app-choice", json=outside, headers={"Origin": BASE}).json()["error"] == "invalid_project"
    dotdot = {**body, "project_dir": f"{ROOT}/garden/../board"}
    assert client.put("/api/previews/app-choice", json=dotdot, headers={"Origin": BASE}).json()["error"] == "invalid_project"
    unknown = {**body, "env": "work9"}
    assert client.put("/api/previews/app-choice", json=unknown, headers={"Origin": BASE}).status_code == 404
    res = client.put("/api/previews/app-choice", json=body, headers={"Origin": BASE})
    assert res.status_code == 200
    group = res.json()["app_choices"][0]
    assert group["state"] == "saved" and group["app"] == "proc:work2:8001:4001"
    assert client.post("/api/previews/scan", headers={"Origin": BASE}).json()["app_choices"][0]["state"] == "saved"
    key = {"env": "work2", "project_dir": garden}
    res = client.request("DELETE", "/api/previews/app-choice", json=key, headers={"Origin": BASE})
    assert res.status_code == 200 and res.json()["app_choices"][0]["state"] == "suggested"
    assert rt.db.one("SELECT * FROM preview_app_choices") is None
