"""Project preview servers.

A preview is a registered development command that relay can start in a
detached tmux session inside a work container, watch for readiness, and
expose through a loopback (and Tailscale) URL. relay only ever manages a
server that it started itself: identity is the tmux session
``(socket, server_pid, session_id, created)`` recorded when it started.

Security notes:

- The dev command is one ``argv`` element (``bash -lc <command>``). It is the
  owner's own command run in their own container. It never becomes a host
  shell string.
- Project directories are resolved with ``realpath -e --`` inside the
  container and confirmed to stay under ``projects_root``.
- Port rules differ per network mode. With slirp4netns (work1) only a
  published port may be used; with host networking (work2) any high port
  except relay's own may be used.
"""

from __future__ import annotations

import asyncio
import re
import socket as socketlib
import sqlite3
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import httpx

from . import activity, preview_discovery
from .config import EnvConfig
from .errors import ApiError
from .podman import ContainerInfo
from .db import Database, dumps, loads, now, row_to_dict
from .runtime import Runtime

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,39}$")
SLUG_RE = re.compile(r"[^A-Za-z0-9]+")

MAX_COMMAND = 500
MAX_HEALTH_PATH = 200
MAX_TEST_COMMAND = 500
MAX_LOG_LINES = 2000

READINESS_TIMEOUT = 90.0
READINESS_INTERVAL = 1.0
RECONCILE_INTERVAL = 5.0
CONNECT_TIMEOUT = 0.5

# preview_servers.status: 'starting' | 'ready' | 'failed' | 'stopped' | 'exited'
LIVE_STATUSES = ("starting", "ready")


# -- validation --------------------------------------------------------------

def slug(name: str) -> str:
    s = SLUG_RE.sub("-", name).strip("-").lower()
    return s or "preview"


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ApiError(400, "invalid_name", "Name must be 1 to 40 characters (letters, digits, space, _ or -).")
    return name


def validate_command(command: str) -> str:
    if not isinstance(command, str):
        raise ApiError(400, "invalid_command", "A command is required.")
    if not (1 <= len(command) <= MAX_COMMAND):
        raise ApiError(400, "invalid_command", f"The command must be 1 to {MAX_COMMAND} characters.")
    if "\x00" in command or "\n" in command or "\r" in command:
        raise ApiError(400, "invalid_command", "The command must not contain a NUL or a newline.")
    return command


def validate_health_path(health_path: str) -> str:
    if not isinstance(health_path, str) or not health_path.startswith("/"):
        raise ApiError(400, "invalid_health_path", "The health path must start with '/'.")
    if len(health_path) > MAX_HEALTH_PATH:
        raise ApiError(400, "invalid_health_path", f"The health path must be at most {MAX_HEALTH_PATH} characters.")
    if "\x00" in health_path or "\n" in health_path or "\r" in health_path:
        raise ApiError(400, "invalid_health_path", "The health path must not contain a NUL or a newline.")
    return health_path


def validate_test_command(test_command: str | None) -> str | None:
    if test_command is None or test_command == "":
        return None
    if not isinstance(test_command, str) or len(test_command) > MAX_TEST_COMMAND:
        raise ApiError(400, "invalid_test_command", f"The test command must be at most {MAX_TEST_COMMAND} characters.")
    if "\x00" in test_command or "\n" in test_command or "\r" in test_command:
        raise ApiError(400, "invalid_test_command", "The test command must not contain a NUL or a newline.")
    return test_command


async def resolve_project(rt: Runtime, env: EnvConfig, project: str | None) -> str:
    """Resolve a project path inside the container. '' means the projects root.

    The path is relative, has no ``..`` segment, resolves with ``realpath -e --``
    inside the container, stays under ``projects_root``, and is a directory.
    """
    root = env.projects_root
    project = (project or "").strip("/")
    if project == "":
        candidate = root
    else:
        if ".." in project.split("/"):
            raise ApiError(400, "invalid_project", "The project path must not contain '..'.")
        if project.startswith("/") or "\x00" in project or "\n" in project:
            raise ApiError(400, "invalid_project", "The project path is not valid.")
        candidate = f"{root}/{project}"
    res = await rt.podman.exec(env, ["realpath", "-e", "--", candidate], timeout=15)
    if not res.ok:
        raise ApiError(400, "invalid_project", "The project directory does not exist.")
    resolved = res.out.strip()
    if resolved != root and not resolved.startswith(root.rstrip("/") + "/"):
        raise ApiError(400, "invalid_project", "The project directory must stay under the projects root.")
    # ``resolved`` is absolute (realpath output), so it cannot be read as an
    # option. Do not pass ``--``: POSIX ``test`` treats it as an operand.
    check = await rt.podman.exec(env, ["test", "-d", resolved], timeout=15)
    if not check.ok:
        raise ApiError(400, "invalid_project", "The project path is not a directory.")
    return resolved


def resolve_host_port(info: ContainerInfo, port: int, own_port: int) -> int:
    """Return the host loopback port for a container port, or raise ApiError.

    With host networking the container port is the host port; only the range
    and relay's own port are checked. With slirp4netns the port must be a
    published port.
    """
    if info.network_mode == "host":
        if not (1024 <= port <= 65535):
            raise ApiError(400, "invalid_port", "The port must be between 1024 and 65535.")
        if port == own_port:
            raise ApiError(400, "invalid_port", "The port must not be relay's own port.")
        return port
    if port not in info.ports:
        available = sorted(info.ports)
        raise ApiError(
            400,
            "port_not_published",
            "The container does not publish this port to the host.",
            available=available,
        )
    return info.ports[port]


def port_listening(host_port: int) -> bool:
    """True when a TCP server accepts a connection on 127.0.0.1:host_port."""
    try:
        with socketlib.create_connection(("127.0.0.1", host_port), timeout=CONNECT_TIMEOUT):
            return True
    except OSError:
        return False


# Read the container's TCP tables. The script body is static; no user text.
_PROC_NET_SCRIPT = "cat /proc/net/tcp /proc/net/tcp6 2>/dev/null; exit 0"


def proc_net_listening(text: str, port: int) -> bool:
    """True when ``/proc/net/tcp``-format text has a LISTEN socket on ``port``."""
    want = f"{port:04X}"
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4 or ":" not in parts[1]:
            continue
        if parts[3] == "0A" and parts[1].rsplit(":", 1)[1].upper() == want:
            return True
    return False


async def server_listening(rt: Runtime, env: EnvConfig, info: ContainerInfo | None, port: int, host_port: int | None) -> bool:
    """True when something listens on the preview's container port.

    With host networking the container shares the host network, so a local
    connect is enough. With slirp4netns, podman's ``rootlessport`` always
    listens on the published host port, even when nothing listens in the
    container, so the container's own TCP table is the only reliable source.
    """
    if info is not None and info.network_mode == "host":
        return port_listening(host_port) if host_port else False
    try:
        res = await rt.podman.exec(env, ["sh", "-c", _PROC_NET_SCRIPT], timeout=10)
    except Exception:  # noqa: BLE001 - unknown means not listening
        return False
    return proc_net_listening(res.out, port)


# -- views -------------------------------------------------------------------

def _server_view(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "status": row["status"],
        "started_at": row["started_at"],
        "ready_at": row["ready_at"],
        "error": row["error"],
    }


def latest_server(db: Database, preview_id: str) -> sqlite3.Row | None:
    return db.one(
        "SELECT * FROM preview_servers WHERE preview_id = ? ORDER BY started_at DESC LIMIT 1",
        (preview_id,),
    )


def live_server(db: Database, preview_id: str) -> sqlite3.Row | None:
    return db.one(
        "SELECT * FROM preview_servers WHERE preview_id = ? AND status IN ('starting','ready')"
        " ORDER BY started_at DESC LIMIT 1",
        (preview_id,),
    )


async def row_host_port(rt: Runtime, row: sqlite3.Row) -> tuple[ContainerInfo, int | None]:
    env = rt.settings.env(row["env"])
    info = rt.env_status[row["env"]].container if row["env"] in rt.env_status else None
    if info is None:
        info = await rt.podman.inspect(env)
    try:
        return info, resolve_host_port(info, int(row["port"]), rt.settings.port)
    except ApiError:
        return info, None


def endpoint_key(env_id: str, port: int, host_port: int | None) -> str:
    """One card per workspace and laptop endpoint (or unpublished container port)."""
    return f"{env_id}:{host_port if host_port else 'c' + str(port)}"


async def preview_view(
    rt: Runtime,
    row: sqlite3.Row,
    *,
    last_check: dict[str, Any] | None = None,
    detected: dict[str, Any] | None = None,
    listen_ports: list[int] | None = None,
    phone: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A registered preview. ``detected``/``listen_ports`` come from a discovery scan."""
    env = rt.settings.env(row["env"])
    info, host_port = await row_host_port(rt, row)
    if not host_port:
        listening = False
    elif listen_ports is not None and (info is None or info.network_mode != "host"):
        # The scan already read the container's TCP table: no extra exec.
        listening = int(row["port"]) in listen_ports
    else:
        listening = await server_listening(rt, env, info, int(row["port"]), host_port)
    server = latest_server(rt.db, row["id"])
    managed = server is not None and server["status"] in LIVE_STATUSES
    urls = build_urls(rt, host_port)
    if phone is not None:
        urls["tailnet"] = phone.get("url") if phone.get("status") == "verified" else None
    return {
        "id": row["id"],
        "env": row["env"],
        "name": row["name"],
        "project_dir": row["project_dir"],
        "command": row["command"],
        "port": row["port"],
        "host_port": host_port,
        "health_path": row["health_path"],
        "test_command": row["test_command"],
        "listening": listening,
        "server": _server_view(server),
        "urls": urls,
        "last_check": last_check,
        "source": "managed" if managed else ("detected" if (detected or listening) else None),
        "detected": detected,
        "phone": phone,
    }


def build_urls(rt: Runtime, host_port: int | None) -> dict[str, str | None]:
    """The laptop URL. ``tailnet`` stays None: only a verified gateway route fills it."""
    if not host_port:
        return {"local": None, "tailnet": None}
    return {"local": f"http://127.0.0.1:{host_port}", "tailnet": None}


# -- CRUD --------------------------------------------------------------------

def get_preview(db: Database, preview_id: str) -> sqlite3.Row:
    row = db.one("SELECT * FROM previews WHERE id = ?", (preview_id,))
    if row is None:
        raise ApiError(404, "not_found", "The preview does not exist.")
    return row


def list_previews_rows(db: Database, *, env: str | None = None) -> list[sqlite3.Row]:
    if env is not None:
        return db.query("SELECT * FROM previews WHERE env = ? ORDER BY name", (env,))
    return db.query("SELECT * FROM previews ORDER BY env, name")


def _name_exists(db: Database, env: str, name: str, *, exclude_id: str | None = None) -> bool:
    row = db.one(
        "SELECT id FROM previews WHERE env = ? AND name = ? AND id != ?",
        (env, name, exclude_id or ""),
    )
    return row is not None


async def create_preview(rt: Runtime, data: dict[str, Any]) -> sqlite3.Row:
    env_id = data.get("env")
    if env_id not in rt.settings.envs:
        raise ApiError(404, "unknown_env", f"Unknown environment: {env_id!r}")
    env = rt.settings.env(env_id)
    name = validate_name(data.get("name", ""))
    command = validate_command(data.get("command", ""))
    health_path = validate_health_path(data.get("health_path", "/"))
    test_command = validate_test_command(data.get("test_command"))
    port = _validate_port_value(data.get("port"))
    info = await rt.podman.inspect(env)
    resolve_host_port(info, port, rt.settings.port)  # validates against the network mode
    project_dir = await resolve_project(rt, env, data.get("project"))
    if _name_exists(rt.db, env_id, name):
        raise ApiError(409, "duplicate_name", "A preview with this name exists in this environment.")
    preview_id = new_id_value()
    ts = now()
    rt.db.insert(
        "previews",
        {
            "id": preview_id,
            "env": env_id,
            "name": name,
            "project_dir": project_dir,
            "command": command,
            "port": port,
            "health_path": health_path,
            "test_command": test_command,
            "created_at": ts,
            "updated_at": ts,
        },
    )
    rt.bus.publish("previews")
    return get_preview(rt.db, preview_id)


async def update_preview(rt: Runtime, preview_id: str, data: dict[str, Any]) -> sqlite3.Row:
    row = get_preview(rt.db, preview_id)
    env = rt.settings.env(row["env"])
    fields: dict[str, Any] = {}
    if "name" in data:
        name = validate_name(data["name"])
        if _name_exists(rt.db, row["env"], name, exclude_id=preview_id):
            raise ApiError(409, "duplicate_name", "A preview with this name exists in this environment.")
        fields["name"] = name
    if "command" in data:
        fields["command"] = validate_command(data["command"])
    if "health_path" in data:
        fields["health_path"] = validate_health_path(data["health_path"])
    if "test_command" in data:
        fields["test_command"] = validate_test_command(data["test_command"])
    if "port" in data:
        port = _validate_port_value(data["port"])
        info = await rt.podman.inspect(env)
        resolve_host_port(info, port, rt.settings.port)
        fields["port"] = port
    if "project" in data:
        fields["project_dir"] = await resolve_project(rt, env, data["project"])
    if fields:
        fields["updated_at"] = now()
        rt.db.update("previews", preview_id, fields)
        rt.bus.publish("previews")
    return get_preview(rt.db, preview_id)


async def delete_preview(rt: Runtime, preview_id: str) -> None:
    row = get_preview(rt.db, preview_id)
    server = live_server(rt.db, preview_id)
    if server is not None and await _server_is_managed_live(rt, row, server):
        raise ApiError(409, "running", "Stop the preview server before you delete the preview.")
    rt.db.execute("DELETE FROM previews WHERE id = ?", (preview_id,))
    rt.bus.publish("previews")


def _validate_port_value(value: Any) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ApiError(400, "invalid_port", "A numeric port is required.") from None
    if not (1 <= port <= 65535):
        raise ApiError(400, "invalid_port", "The port must be between 1 and 65535.")
    return port


# -- start / stop ------------------------------------------------------------

async def _server_is_managed_live(rt: Runtime, preview_row: sqlite3.Row, server: sqlite3.Row) -> bool:
    """True when the recorded tmux session for a server still exists."""
    env = rt.settings.env(preview_row["env"])
    tmux = rt.tmux[preview_row["env"]]
    session_id = server["tmux_session_id"]
    if not session_id:
        return False
    try:
        snap = await tmux.snapshot(sockets=[server["socket_path"]])
    except Exception:
        return False
    found = snap.find(server["socket_path"], session_id)
    if found is None:
        return False
    return found.created == server["tmux_created"] and found.server_pid == server["server_pid"]


async def start_preview(rt: Runtime, preview_id: str) -> sqlite3.Row:
    async with rt.lock_for(f"preview:{preview_id}"):
        row = get_preview(rt.db, preview_id)
        env = rt.settings.env(row["env"])
        tmux = rt.tmux[row["env"]]
        info = await rt.podman.inspect(env)
        host_port = resolve_host_port(info, int(row["port"]), rt.settings.port)
        existing = live_server(rt.db, preview_id)
        if existing is not None and await _server_is_managed_live(rt, row, existing):
            raise ApiError(409, "already_running", "The preview server is already running.", managed=True)
        if await server_listening(rt, env, info, int(row["port"]), host_port):
            raise ApiError(409, "port_in_use", "Another server already listens on this port.", managed=False)
        name = f"preview-{slug(row['name'])}"
        command = row["command"]
        session_id, pane_id, created, server_pid = await tmux.new_session(
            env.previews_socket,
            name=name,
            cwd=row["project_dir"],
            argv=["bash", "-lc", 'umask 077; exec "$@"', "relay-preview", "bash", "-lc", command],
        )
        server_row_id = new_id_value()
        ts = now()
        rt.db.insert(
            "preview_servers",
            {
                "id": server_row_id,
                "preview_id": preview_id,
                "status": "starting",
                "socket_path": env.previews_socket,
                "server_pid": server_pid,
                "tmux_session_id": session_id,
                "tmux_created": created,
                "started_at": ts,
                "ready_at": None,
                "stopped_at": None,
                "error": None,
            },
        )
        _publish_preview_change(rt, row, "starting")
        spawn(rt, readiness_task(rt, preview_id, server_row_id, host_port))
        return get_preview(rt.db, preview_id)


async def stop_preview(rt: Runtime, preview_id: str) -> sqlite3.Row:
    async with rt.lock_for(f"preview:{preview_id}"):
        row = get_preview(rt.db, preview_id)
        env = rt.settings.env(row["env"])
        tmux = rt.tmux[row["env"]]
        server = live_server(rt.db, preview_id)
        if server is None or not await _server_is_managed_live(rt, row, server):
            raise ApiError(409, "not_managed", "relay does not manage a live server for this preview.")
        try:
            await tmux.kill_session(server["socket_path"], server["tmux_session_id"])
        except Exception:
            pass
        rt.db.update(
            "preview_servers",
            server["id"],
            {"status": "stopped", "stopped_at": now()},
        )
        _publish_preview_change(rt, row, "stopped")
        return get_preview(rt.db, preview_id)


async def preview_logs(rt: Runtime, preview_id: str, lines: int = 200) -> str:
    row = get_preview(rt.db, preview_id)
    tmux = rt.tmux[row["env"]]
    server = latest_server(rt.db, preview_id)
    if server is None or not server["tmux_session_id"]:
        return ""
    lines = max(1, min(int(lines), MAX_LOG_LINES))
    try:
        return await _capture_session(rt, row, server, lines)
    except Exception:
        return ""


async def _capture_session(rt: Runtime, preview_row: sqlite3.Row, server: sqlite3.Row, lines: int) -> str:
    """Capture the target pane of a preview tmux session."""
    tmux = rt.tmux[preview_row["env"]]
    snap = await tmux.snapshot(sockets=[server["socket_path"]])
    found = snap.find(server["socket_path"], server["tmux_session_id"])
    if found is None or found.target_pane is None:
        return ""
    return await tmux.capture(server["socket_path"], found.target_pane.pane_id, lines=lines)


# -- readiness and reconciliation --------------------------------------------

async def readiness_task(rt: Runtime, preview_id: str, server_row_id: str, host_port: int) -> None:
    """Poll the health URL until ready, the session vanishes, or the timeout."""
    row = rt.db.one("SELECT * FROM previews WHERE id = ?", (preview_id,))
    if row is None:
        return
    health_path = row["health_path"]
    url = f"http://127.0.0.1:{host_port}{health_path}"
    deadline = now() + READINESS_TIMEOUT
    async with httpx.AsyncClient(timeout=CONNECT_TIMEOUT + 1.5) as client:
        while now() < deadline:
            server = rt.db.one("SELECT * FROM preview_servers WHERE id = ?", (server_row_id,))
            if server is None or server["status"] not in LIVE_STATUSES:
                return  # stopped or already resolved
            if not await _server_is_managed_live(rt, row, server):
                error = await _tail_logs(rt, row, server)
                _finish_server(rt, row, server_row_id, "exited", error=error)
                return
            try:
                resp = await client.get(url)
                if resp.status_code < 500:
                    _finish_server(rt, row, server_row_id, "ready")
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(READINESS_INTERVAL)
    # Timeout: the server may still run, but readiness failed.
    _finish_server(rt, row, server_row_id, "failed", error="Readiness timed out after 90 s.")


async def _tail_logs(rt: Runtime, preview_row: sqlite3.Row, server: sqlite3.Row, lines: int = 40) -> str | None:
    try:
        text = await _capture_session(rt, preview_row, server, lines)
    except Exception:
        return None
    text = text.strip()
    return text[-2000:] if text else None


def _finish_server(rt: Runtime, preview_row: sqlite3.Row, server_row_id: str, status: str, *, error: str | None = None) -> None:
    fields: dict[str, Any] = {"status": status}
    if status == "ready":
        fields["ready_at"] = now()
    if status in ("exited", "failed", "stopped"):
        fields["stopped_at"] = now()
    if error is not None:
        fields["error"] = error
    rt.db.update("preview_servers", server_row_id, fields)
    _publish_preview_change(rt, preview_row, status)


def _publish_preview_change(rt: Runtime, preview_row: sqlite3.Row, status: str) -> None:
    rt.bus.publish("previews")
    attention = status in ("failed", "exited")
    title = f"Preview {preview_row['name']}: {status}"
    activity.record(
        rt.db,
        kind="preview",
        title=title,
        session_id=None,
        env=preview_row["env"],
        attention=attention,
        dedupe_key=None,
        bus=rt.bus,
    )


async def reconcile_once(rt: Runtime) -> None:
    """Mark every live server row whose tmux session vanished as 'exited'.

    Never starts or restarts a server.
    """
    rows = rt.db.query(
        "SELECT s.*, p.env AS p_env, p.name AS p_name FROM preview_servers s"
        " JOIN previews p ON p.id = s.preview_id WHERE s.status IN ('starting','ready')"
    )
    for server in rows:
        preview_row = rt.db.one("SELECT * FROM previews WHERE id = ?", (server["preview_id"],))
        if preview_row is None:
            continue
        if not await _server_is_managed_live(rt, preview_row, server):
            _finish_server(rt, preview_row, server["id"], "exited", error="The tmux session is gone.")


async def run_forever(rt: Runtime) -> None:
    """Reconcile once at startup, then every 5 s. Never restart a server."""
    try:
        await reconcile_once(rt)
    except Exception:
        pass
    while True:
        await asyncio.sleep(RECONCILE_INTERVAL)
        try:
            await reconcile_once(rt)
        except Exception:
            pass


# -- app choices -------------------------------------------------------------
#
# The Previews page shows one primary app per workspace and project group. The
# group of a server is the nearest folder with a .git marker under the projects
# root (preview_discovery.git_group), else the longest registered project folder
# that holds it, else its own verified folder. A server without a verified
# folder joins no group: it stays in the technical list only.
#
# Per group: a saved choice wins. Without one, exactly one clear frontend
# (preview_discovery.is_frontend) is a suggestion that relay does not save.
# Every other group needs a choice. A saved choice matches a current candidate
# only inside the same group, by identity:
#
# - registered: the preview id (the port can change; the group cannot);
# - process: the source folder, the container and laptop ports, and the tool,
#   and for an unknown tool also the PID and start time, so a new process on
#   the port never binds;
# - docker: the container name and both ports.
#
# A saved choice without a match shows as stopped. relay never moves it to
# another server. Its snapshot is display text only and authorizes nothing.

MAX_CHOICE_PATH = 4096


def _under_root(env: EnvConfig, path: str) -> bool:
    root = env.projects_root.rstrip("/")
    return path == root or (path.startswith(root + "/") and ".." not in PurePosixPath(path).parts)


def _rel_project(env: EnvConfig, path: str) -> str:
    root = env.projects_root.rstrip("/")
    return "projects" if path == root else path[len(root) + 1:]


def _registered_status(view: dict[str, Any]) -> str:
    status = (view.get("server") or {}).get("status")
    if status == "starting":
        return "starting"
    if status == "ready" or view.get("listening") or view.get("detected"):
        return "running"
    return "stopped"


def _detected_folder(env: EnvConfig, endpoint: dict[str, Any]) -> str | None:
    """The verified folder of a detected server: a process cwd or a Docker link."""
    project = endpoint.get("project")
    if not project:
        return None  # unknown, or the projects root itself
    if endpoint.get("source") == "process":
        return endpoint.get("folder")
    return env.projects_root.rstrip("/") + "/" + project


def app_candidates(rt: Runtime, preview_views: list[dict[str, Any]], detected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every current server as a candidate, with its group (None when unknown).

    ``_endpoint`` is internal: the detected endpoint for identity checks.
    """
    out: list[dict[str, Any]] = []
    parents: dict[str, list[str]] = {}
    for p in preview_views:
        env = rt.settings.env(p["env"])
        folder = p["project_dir"]
        group = None
        if _under_root(env, folder):
            group = preview_discovery.git_group(env, folder) or folder
            if folder != env.projects_root.rstrip("/"):
                parents.setdefault(p["env"], []).append(folder)
        endpoint = p.get("detected")
        tool = ((endpoint or {}).get("process") or {}).get("tool")
        out.append(
            {
                "id": f"reg:{p['id']}", "kind": "registered", "env": p["env"], "group": group,
                "tool": tool, "frontend": preview_discovery.is_frontend(endpoint), "name": p["name"],
                "port": p["port"], "host_port": p["host_port"], "status": _registered_status(p),
                "preview_id": p["id"], "endpoint_key": (endpoint or {}).get("key"), "_endpoint": endpoint,
            }
        )
    for d in detected:
        env = rt.settings.env(d["env"])
        folder = _detected_folder(env, d)
        group = None
        if folder is not None and _under_root(env, folder):
            holders = [p for p in parents.get(d["env"], []) if folder == p or folder.startswith(p + "/")]
            group = preview_discovery.git_group(env, folder) or max(holders, key=len, default=None) or folder
        proc = d.get("process") or {}
        if d.get("source") == "docker":
            cid, tool, name = f"docker:{d['key']}:{d['docker']['name']}", None, d["docker"]["name"]
        else:
            cid, tool, name = f"proc:{d['key']}:{proc.get('pid')}", proc.get("tool"), proc.get("name")
        out.append(
            {
                "id": cid, "kind": "docker" if d.get("source") == "docker" else "process", "env": d["env"],
                "group": group, "tool": tool, "frontend": preview_discovery.is_frontend(d), "name": name,
                "port": d["port"], "host_port": d["host_port"], "status": "running", "preview_id": None,
                "endpoint_key": d["key"], "_endpoint": d,
            }
        )
    return out


def _identity(candidate: dict[str, Any]) -> dict[str, Any]:
    if candidate["kind"] == "registered":
        return {"source": "registered", "preview_id": candidate["preview_id"]}
    e = candidate["_endpoint"]
    if e.get("source") == "docker":
        return {"source": "docker", "name": e["docker"]["name"], "host_port": e["host_port"],
                "container_port": e["docker"]["container_port"]}
    proc = e.get("process") or {}
    identity = {"source": "process", "port": e["port"], "host_port": e["host_port"], "tool": proc.get("tool"),
                "folder": e.get("folder")}
    if identity["tool"] is None:
        identity.update(pid=proc.get("pid"), started_at=proc.get("started_at"))
    return identity


def _endpoint_matches(identity: dict[str, Any], e: dict[str, Any] | None) -> bool:
    if e is None:
        return False
    if identity.get("source") == "docker":
        docker = e.get("docker") or {}
        return (
            e.get("source") == "docker" and docker.get("name") == identity.get("name")
            and e.get("host_port") == identity.get("host_port") and docker.get("container_port") == identity.get("container_port")
        )
    if identity.get("source") != "process" or e.get("source") != "process":
        return False
    proc = e.get("process") or {}
    if e.get("folder") is None or e.get("folder") != identity.get("folder"):
        return False  # the same tool and port in another folder is another server
    if e.get("port") != identity.get("port") or e.get("host_port") != identity.get("host_port"):
        return False
    if proc.get("tool") != identity.get("tool"):
        return False
    if identity.get("tool") is None:
        return identity.get("pid") is not None and proc.get("pid") == identity.get("pid") and proc.get("started_at") == identity.get("started_at")
    return True


def _match(identity: dict[str, Any], members: list[dict[str, Any]]) -> dict[str, Any] | None:
    if identity.get("source") == "registered":
        return next((c for c in members if c["kind"] == "registered" and c["preview_id"] == identity.get("preview_id")), None)
    # A registered preview that claims the endpoint carries it as its own.
    return next((c for c in members if _endpoint_matches(identity, c["_endpoint"])), None)


def _public(candidate: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in candidate.items() if k not in ("_endpoint", "group", "env")}


def app_groups(rt: Runtime, preview_views: list[dict[str, Any]], detected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per workspace and project group with candidates or a saved choice."""
    members: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for c in app_candidates(rt, preview_views, detected):
        if c["group"]:
            members.setdefault((c["env"], c["group"]), []).append(c)
    saved = {(r["env"], r["project_dir"]): r for r in rt.db.query("SELECT * FROM preview_app_choices")}
    out = []
    for env_id, folder in sorted(set(members) | set(saved)):
        if env_id not in rt.settings.envs:
            continue
        env = rt.settings.env(env_id)
        if not _under_root(env, folder):
            continue
        group = sorted(members.get((env_id, folder), []), key=lambda c: (not c["frontend"], c["kind"] != "registered", c["port"]))
        row = saved.get((env_id, folder))
        state, app, saved_view = "needs_choice", None, None
        if row is not None:
            match = _match(loads(row["identity_json"]) or {}, group)
            state, app = "saved", match["id"] if match else None
            saved_view = {"kind": row["kind"], "snapshot": loads(row["snapshot_json"]) or {},
                          "chosen_at": row["chosen_at"], "available": match is not None}
        else:
            likely = [c for c in group if c["frontend"]]
            if len(likely) == 1:
                state, app = "suggested", likely[0]["id"]
        out.append(
            {
                "key": f"{env_id}:{folder}", "env": env_id, "project_dir": folder, "project": _rel_project(env, folder),
                "state": state, "app": app, "saved": saved_view, "candidates": [_public(c) for c in group],
            }
        )
    return out


def _check_choice_key(rt: Runtime, env_id: str, project_dir: str) -> EnvConfig:
    if env_id not in rt.settings.envs:
        raise ApiError(404, "unknown_env", f"Unknown environment: {env_id!r}")
    env = rt.settings.env(env_id)
    if (
        not isinstance(project_dir, str) or len(project_dir) > MAX_CHOICE_PATH or "\x00" in project_dir
        or "\n" in project_dir or str(PurePosixPath(project_dir)) != project_dir or not _under_root(env, project_dir)
    ):
        raise ApiError(400, "invalid_project", "The project folder must stay under the projects root.")
    return env


def save_app_choice(
    rt: Runtime, preview_views: list[dict[str, Any]], detected: list[dict[str, Any]],
    env_id: str, project_dir: str, candidate_id: str,
) -> None:
    """Save one candidate of the current inventory as the app of its group.

    The client names a candidate id only. relay finds it in the current
    inventory, confirms its workspace and group, and stores the identity it
    computes itself. A URL is never accepted or stored.
    """
    _check_choice_key(rt, env_id, project_dir)
    cand = next((c for c in app_candidates(rt, preview_views, detected) if c["id"] == candidate_id), None)
    if cand is None:
        raise ApiError(409, "candidate_gone", "relay does not see this server now. Rescan, then choose again.")
    if cand["env"] != env_id or cand["group"] != project_dir:
        raise ApiError(409, "wrong_project", "This server does not belong to this project.")
    snapshot = {"tool": cand["tool"], "name": cand["name"], "port": cand["port"], "host_port": cand["host_port"]}
    try:
        rt.db.execute(
            "INSERT INTO preview_app_choices (env, project_dir, kind, preview_id, identity_json, snapshot_json, chosen_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(env, project_dir) DO UPDATE SET kind = excluded.kind,"
            " preview_id = excluded.preview_id, identity_json = excluded.identity_json,"
            " snapshot_json = excluded.snapshot_json, chosen_at = excluded.chosen_at",
            (env_id, project_dir, cand["kind"], cand["preview_id"], dumps(_identity(cand)), dumps(snapshot), now()),
        )
    except sqlite3.IntegrityError:  # the registered preview was removed meanwhile
        raise ApiError(409, "candidate_gone", "relay does not see this server now. Rescan, then choose again.") from None
    rt.bus.publish("previews")


def delete_app_choice(rt: Runtime, env_id: str, project_dir: str) -> bool:
    _check_choice_key(rt, env_id, project_dir)
    cur = rt.db.execute("DELETE FROM preview_app_choices WHERE env = ? AND project_dir = ?", (env_id, project_dir))
    if cur.rowcount:
        rt.bus.publish("previews")
    return cur.rowcount > 0


# -- small helpers -----------------------------------------------------------

def new_id_value() -> str:
    from .db import new_id

    return new_id()


def spawn(rt: Runtime, coro: Any) -> asyncio.Task[Any]:
    """Run a coroutine in the background and drop it from rt.background when done."""
    task = asyncio.ensure_future(coro)
    rt.background.append(task)
    task.add_done_callback(lambda t: rt.background.remove(t) if t in rt.background else None)
    return task


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return row_to_dict(row)
