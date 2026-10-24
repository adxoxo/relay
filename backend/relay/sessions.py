"""Session views and lifecycle operations.

This module builds the read views (``env_view``, ``session_view``,
``state_view``) and performs the write operations (create, end, prompt,
interrupt, resume). Live tmux inventory is the source of truth for existence;
the ``sessions`` table stores dashboard metadata.

Security rules (ARCHITECTURE.md section 10):

- All tmux and podman work uses argv lists. User text (names, projects,
  prompts) is never part of a host or container shell string.
- Every id is validated against its pattern before use.
- A project path is resolved with ``realpath -e`` inside the container and
  confined under the environment's ``projects_root``.
"""

from __future__ import annotations

import re
import socket
import sqlite3
import uuid
from typing import Any

from . import activity, codex_runtime, inventory, screen, terminal_history
from .config import EnvConfig
from .db import new_id, now
from .errors import ApiError
from .runtime import Runtime

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,39}$")
MAX_PROMPT = 8000
PERMISSION_MODES = ("manual", "acceptEdits", "auto", "plan")
_AGENT_KINDS = frozenset({"claude", "codex"})
_KINDS = frozenset({"claude", "codex", "shell"})
_RECENT_WINDOW = 24 * 3600  # ended/interrupted rows shown for this long


# -- views -------------------------------------------------------------------


def env_view(rt: Runtime, env_id: str) -> dict[str, Any]:
    env = rt.settings.env(env_id)
    st = rt.env_status.get(env_id)
    info = st.container if st else None
    snapshot_at = st.snapshot_at if st else None
    error = st.error if st else None

    if info is None:
        status = "error" if error else "stopped"
        network = None
    elif not info.exists:
        status = "missing" if info.status == "missing" else "error"
        network = None
    elif info.running:
        status = "running"
        network = info.network_mode
    else:
        status = "stopped"
        network = info.network_mode

    provision = st.provision if st else None
    provisioned = bool(provision and provision.get("ok")) if provision is not None else False

    return {
        "id": env_id,
        "label": env.label,
        "container": env.container,
        "available": status == "running",
        "status": status,
        "network": network,
        "projects_root": env.projects_root,
        "error": error,
        "snapshot_at": snapshot_at,
        "agents": inventory.agents_for(rt, env_id),
        "provisioned": provisioned,
    }


def _capabilities(row: sqlite3.Row, kind: str, live: bool, agent_fg: bool) -> dict[str, bool]:
    pending = _pending(row)
    conversation = kind in _AGENT_KINDS and bool(row["agent_session_id"]) and bool(row["transcript_path"])
    if conversation and kind == "codex":
        try:
            terminal_history._check_codex_binding(row, row["env"], row["agent_session_id"], row["transcript_path"])
        except terminal_history.HistoryUnavailable:
            conversation = False
    return {
        "terminal": live,
        "prompt": live and kind in _AGENT_KINDS and agent_fg,
        "approve": kind == "claude" and pending is not None and bool(pending.get("answerable")),
        "interrupt": live and kind in _AGENT_KINDS and agent_fg,
        "conversation": conversation,
        "resume": not live and kind in _AGENT_KINDS and bool(row["agent_session_id"]),
        "end": live,
    }


def _notes(row: sqlite3.Row, kind: str, live: bool, agent_fg: bool) -> list[str]:
    notes: list[str] = []
    linked = kind == "codex" and codex_runtime.is_verified(codex_runtime.binding_of(row))
    if kind == "shell":
        notes.append("Shell sessions have no agent status, prompt, or conversation.")
    if row["origin"] == "discovered" and kind in _AGENT_KINDS and not linked:
        notes.append("Status comes from the terminal only. Start agents from relay for live status.")
    if linked:
        notes.append("Status follows the linked Codex thread. After /new or /resume inside Codex, relay links the new thread when Codex has saved it.")
    if kind in _AGENT_KINDS and live and not agent_fg:
        notes.append("The agent is not in the foreground of its pane, so prompts are unavailable.")
    if kind == "codex" and not linked:
        notes.append("Conversation and saved terminal history need a verified link to this Codex pane and thread.")
    return notes


def _pending(row: sqlite3.Row) -> dict[str, Any] | None:
    from .db import loads

    return loads(row["pending_json"])


def _agent_foreground(row: sqlite3.Row, kind: str) -> bool:
    """True when the target pane's foreground command is the agent CLI."""
    if kind not in _AGENT_KINDS:
        return False
    fg = row["foreground"]
    return fg == kind


def session_view(rt: Runtime, row: sqlite3.Row) -> dict[str, Any]:
    from .db import loads

    env = rt.settings.env(row["env"]) if row["env"] in rt.settings.envs else None
    kind = row["kind"]
    live = row["status"] == "live"
    agent_fg = _agent_foreground(row, kind)
    project_dir = row["project_dir"]
    project = None
    if project_dir and env is not None:
        root = env.projects_root
        if project_dir == root:
            project = ""
        elif project_dir.startswith(root + "/"):
            project = project_dir[len(root) + 1 :]

    return {
        "id": row["id"],
        "env": row["env"],
        "env_label": env.label if env else row["env"],
        "name": row["display_name"],
        "tmux_name": row["tmux_name"],
        "kind": kind,
        "origin": row["origin"],
        "status": row["status"],
        "state": row["state"],
        "state_source": row["state_source"],
        "state_at": row["state_at"],
        "project_dir": project_dir,
        "project": project,
        "last_message": row["last_message"],
        "activity_at": row["activity_at"],
        "foreground": row["foreground"],
        "pending": _pending(row),
        "agent_session_id": row["agent_session_id"],
        "created_at": row["created_at"],
        "ended_at": row["ended_at"],
        "end_reason": row["end_reason"],
        "tmux": {
            "session_id": row["tmux_session_id"],
            "created": row["tmux_created"],
            "pane_id": row["pane_id"],
            "pane_count": row["pane_count"],
            "socket": row["socket_path"],
            "server_pid": row["server_pid"],
        },
        "capabilities": _capabilities(row, kind, live, agent_fg),
        "notes": _notes(row, kind, live, agent_fg),
    }


def state_view(rt: Runtime) -> dict[str, Any]:
    envs = [env_view(rt, env_id) for env_id in rt.settings.envs]
    cutoff = now() - _RECENT_WINDOW
    rows = rt.db.query(
        "SELECT * FROM sessions WHERE status = 'live'"
        " OR (status IN ('ended', 'interrupted') AND ended_at IS NOT NULL AND ended_at >= ?)"
        " ORDER BY (status = 'live') DESC, COALESCE(ended_at, created_at) DESC",
        (cutoff,),
    )
    sessions = [session_view(rt, r) for r in rows]

    working = sum(1 for s in sessions if s["status"] == "live" and s["state"] == "working")
    needs_input = sum(1 for s in sessions if s["status"] == "live" and s["state"] == "needs_input")
    finished_unread = activity.unread_count(rt.db, kind="turn_complete")
    by_env: dict[str, int] = {env_id: 0 for env_id in rt.settings.envs}
    for s in sessions:
        if s["status"] == "live":
            by_env[s["env"]] = by_env.get(s["env"], 0) + 1
    live_count = sum(1 for s in sessions if s["status"] == "live")

    return {
        "generated_at": now(),
        "version": "0.1.0",
        "host": {"name": socket.gethostname()},
        "tailscale_hostname": rt.settings.tailscale_hostname,
        "envs": envs,
        "sessions": sessions,
        "counts": {
            "sessions": live_count,
            "working": working,
            "needs_input": needs_input,
            "finished_unread": finished_unread,
            "by_env": by_env,
        },
        "activity_unread": activity.unread_count(rt.db),
    }


# -- helpers -----------------------------------------------------------------


def get_row_or_404(rt: Runtime, session_id: str) -> sqlite3.Row:
    row = rt.db.get_session(session_id)
    if row is None:
        raise ApiError(404, "not_found", "Unknown session")
    return row


def require_env_up(rt: Runtime, env_id: str) -> EnvConfig:
    if env_id not in rt.settings.envs:
        raise ApiError(404, "unknown_env", f"Unknown environment: {env_id}")
    st = rt.env_status.get(env_id)
    if st is None or st.container is None or not st.container.running:
        raise ApiError(503, "env_unavailable", "The environment is not running")
    return rt.settings.env(env_id)


def validate_name(name: str) -> str:
    name = name.strip()
    if not NAME_RE.match(name):
        raise ApiError(400, "invalid_name", "Names use letters, digits, space, underscore, hyphen (max 40)")
    return name


def _live_names(rt: Runtime, env_id: str) -> set[str]:
    """Case-insensitive live names from the DB and the fresh snapshot."""
    names: set[str] = set()
    for row in rt.db.list_sessions(env=env_id, statuses=("live",)):
        names.add(row["display_name"].casefold())
        names.add(row["tmux_name"].casefold())
    st = rt.env_status.get(env_id)
    if st and st.snapshot:
        sockets = set(rt.settings.env(env_id).tmux_sockets)
        for s in st.snapshot.sessions:
            if s.socket in sockets:
                names.add(s.name.casefold())
    return names


def next_agent_name(rt: Runtime, env_id: str) -> str:
    taken = _live_names(rt, env_id)
    n = 1
    while f"agent{n}".casefold() in taken:
        n += 1
    return f"agent{n}"


async def resolve_project(rt: Runtime, env: EnvConfig, project: str) -> str:
    """Resolve a project directory inside the container, confined to the root.

    ``project`` == "" means the projects root. Otherwise it is a relative path
    without ``..`` segments, resolved with ``realpath -e`` and required to be a
    directory equal to the root or under ``root + "/"``.
    """
    root = env.projects_root
    project = (project or "").strip().strip("/")
    if project == "":
        return root
    parts = project.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ApiError(400, "invalid_project", "The project path is invalid")
    candidate = f"{root}/{project}"
    # realpath -e resolves symlinks and requires existence. Path is a positional
    # argument, never part of the script body.
    res = await rt.podman.exec(
        env,
        ["sh", "-c", 'p=$(realpath -e -- "$1") || exit 3; [ -d "$p" ] || exit 4; printf %s "$p"', "relay-project", candidate],
        timeout=15,
    )
    if not res.ok:
        raise ApiError(400, "invalid_project", "The project directory was not found")
    resolved = res.out.strip()
    if resolved != root and not resolved.startswith(root + "/"):
        raise ApiError(400, "invalid_project", "The project directory is outside the projects root")
    return resolved


async def list_projects(rt: Runtime, env_id: str) -> dict[str, Any]:
    env = require_env_up(rt, env_id)
    root = env.projects_root
    # List depth-1 directories, no dot-dirs. Names come back as bare basenames.
    res = await rt.podman.exec(
        env,
        [
            "sh",
            "-c",
            'cd "$1" 2>/dev/null || exit 0; for d in */; do [ -d "$d" ] || continue; printf "%s\\n" "${d%/}"; done',
            "relay-projects",
            root,
        ],
        timeout=15,
    )
    if not res.ok:
        raise ApiError(502, "env_command_failed", "Could not list projects")
    names = sorted(
        {line.strip() for line in res.out.splitlines() if line.strip() and not line.strip().startswith(".")}
    )[:500]
    return {"root": root, "projects": [{"name": n, "path": f"{root}/{n}"} for n in names]}


def build_agent_argv(
    env: EnvConfig,
    kind: str,
    *,
    relay_id: str,
    name: str,
    permission_mode: str | None,
    prompt: str | None,
    notify_override: str | None,
) -> list[str]:
    """Argv for the agent CLI. Prompt is always the last element after ``--``."""
    if kind == "claude":
        argv = [
            "claude",
            "--session-id",
            str(uuid.UUID(relay_id)),
            "--name",
            name,
            "--settings",
            env.claude_settings,
        ]
        if permission_mode is not None:
            argv += ["--permission-mode", permission_mode]
        if prompt:
            argv += ["--", prompt]
        return argv
    if kind == "codex":
        argv = ["codex", "-c", notify_override or ""]
        if prompt:
            argv += ["--", prompt]
        return argv
    if kind == "shell":
        return ["bash", "-l"]
    raise ApiError(400, "invalid_kind", f"Unknown kind: {kind}")


# The agent runs as a child of a small bash wrapper instead of replacing it.
# ``set -m`` puts the agent in its own foreground process group, so tmux still
# reports the agent (not bash) as the pane command while it runs. A clean exit
# (code 0) ends the tmux session as before. A failed start (non-zero code)
# prints one relay line and keeps the tmux session open with a login shell, so
# the error stays readable in the terminal instead of vanishing in a second.
_AGENT_LAUNCH_SCRIPT = (
    'set -m; umask 077; "$@"; rc=$?; '
    'if [ "$rc" -ne 0 ]; then '
    'printf "\\n[relay] %s exited with code %s. The terminal stays open.\\n" "$1" "$rc"; '
    "exec bash -l; fi"
)
_SHELL_LAUNCH_SCRIPT = 'umask 077; exec "$@"'


def _launch_wrapper(agent_argv: list[str]) -> list[str]:
    script = _SHELL_LAUNCH_SCRIPT if agent_argv[:1] == ["bash"] else _AGENT_LAUNCH_SCRIPT
    return ["bash", "-lc", script, "relay-launch", *agent_argv]


# -- operations --------------------------------------------------------------


async def create_session(
    rt: Runtime,
    *,
    env_id: str,
    kind: str,
    name: str | None,
    project: str | None,
    prompt: str | None,
    permission_mode: str | None,
) -> dict[str, Any]:
    if kind not in _KINDS:
        raise ApiError(400, "invalid_kind", "kind must be claude, codex, or shell")
    env = require_env_up(rt, env_id)

    if kind in _AGENT_KINDS and not inventory.agents_for(rt, env_id).get(kind, False):
        raise ApiError(400, "kind_unavailable", f"{kind} is not installed in {env_id}")

    if permission_mode is not None and permission_mode not in PERMISSION_MODES:
        raise ApiError(400, "invalid_request", "Unknown permission mode")
    if kind != "claude":
        permission_mode = None

    prompt = prompt or None
    if prompt is not None:
        if len(prompt) > MAX_PROMPT:
            raise ApiError(400, "invalid_prompt", "The prompt is too long")
        if kind == "shell":
            prompt = None  # a shell has no initial prompt

    codex_notify = None
    if kind == "codex":
        try:
            from .provision import codex_notify_override as codex_notify
        except Exception:  # noqa: BLE001 - adapters module may not be ready
            raise ApiError(503, "env_unavailable", "Codex support is not provisioned") from None

    project_dir = await resolve_project(rt, env, project or "")

    async with rt.lock_for("env:" + env_id):
        # Re-check duplicate names under the env lock to stop create races.
        chosen = validate_name(name) if name and name.strip() else next_agent_name(rt, env_id)
        if chosen.casefold() in _live_names(rt, env_id):
            raise ApiError(409, "duplicate_name", f"A live session called {chosen} already exists")

        relay_id = new_id()
        # The relay id goes into the notify argv: the shared Codex daemon runs
        # notify without this session's environment.
        notify_override = codex_notify(env, relay_id) if codex_notify is not None else None
        agent_argv = build_agent_argv(
            env,
            kind,
            relay_id=relay_id,
            name=chosen,
            permission_mode=permission_mode,
            prompt=prompt,
            notify_override=notify_override,
        )
        socket = env.tmux_sockets[0]
        agent_session_id = str(uuid.UUID(relay_id)) if kind == "claude" else None

        rt.db.insert_session(
            {
                "id": relay_id,
                "env": env_id,
                "kind": kind,
                "display_name": chosen,
                "tmux_name": chosen,
                "origin": "dashboard",
                "status": "live",
                "state": "unknown",
                "state_source": None,
                "socket_path": socket,
                "project_dir": project_dir,
                "initial_prompt": prompt,
                "agent_session_id": agent_session_id,
                "meta_json": _meta({"permission_mode": permission_mode}),
            }
        )
        try:
            session_id, pane_id, created, server_pid = await rt.tmux_for(env_id).new_session(
                socket,
                name=chosen,
                cwd=project_dir,
                argv=_launch_wrapper(agent_argv),
                env_vars={"RELAY_SESSION_ID": relay_id, "RELAY_ENV": env_id},
                relay_id=relay_id,
            )
        except Exception as exc:  # noqa: BLE001 - roll back the row on launch failure
            rt.db.update_session(relay_id, status="ended", state="exited", end_reason="closed", ended_at=now())
            raise ApiError(502, "env_command_failed", f"Could not start the session: {exc}") from exc

        rt.db.update_session(
            relay_id,
            tmux_session_id=session_id,
            pane_id=pane_id,
            tmux_created=created,
            server_pid=server_pid,
        )

    activity.record(
        rt.db,
        kind="session_created",
        title=f"{chosen} started",
        session_id=relay_id,
        env=env_id,
        attention=False,
        dedupe_key=f"created:{relay_id}",
        bus=rt.bus,
    )
    # Reconcile so the new row settles through @relay_id and gets pane data.
    await inventory.refresh_env(rt, env_id)
    row = get_row_or_404(rt, relay_id)
    if kind in _AGENT_KINDS:
        start_startup_watch(rt, relay_id)
    return session_view(rt, row)


# -- startup watch -------------------------------------------------------------

STARTUP_WATCH_SECONDS = 45.0
STARTUP_WATCH_INTERVAL = 1.5


def start_startup_watch(rt: Runtime, session_id: str) -> None:
    """Watch a freshly launched agent for a folder trust dialog (background task)."""
    import asyncio

    task = asyncio.get_running_loop().create_task(watch_startup(rt, session_id))
    rt.background.append(task)
    task.add_done_callback(lambda t: rt.background.remove(t) if t in rt.background else None)


async def watch_startup(
    rt: Runtime,
    session_id: str,
    *,
    duration: float = STARTUP_WATCH_SECONDS,
    interval: float = STARTUP_WATCH_INTERVAL,
    answer_timeout: float = 900.0,
) -> None:
    """Surface a folder trust dialog as a request to answer in the terminal.

    A new folder makes the agent ask for trust before it starts, and the
    agent's hooks stay silent until then. Without this, relay would show the
    session as "unknown" and a prompt could be typed into the dialog. The
    pending item blocks prompts until an agent event (SessionStart) clears it.
    """
    import asyncio

    from .db import dumps, loads

    loop = asyncio.get_running_loop()
    deadline = loop.time() + duration
    pending: dict[str, Any] | None = None
    while loop.time() < deadline:
        await asyncio.sleep(interval)
        row = rt.db.get_session(session_id)
        if row is None or row["status"] != "live" or row["state_source"] == "hook" or row["pending_json"]:
            return
        if not row["pane_id"]:
            continue
        capture = await _capture(rt, row, row["pane_id"], row["socket_path"])
        if capture is None:
            continue
        failure = screen.launch_failure(capture)
        if failure is not None:
            await _report_launch_failure(rt, row, failure)
            return
        if not screen.trust_dialog_visible(capture):
            continue
        pending = {
            "id": new_id()[:16],
            "source": "screen",
            "tool": "Folder trust",
            "summary": row["project_dir"] or "",
            "detail": "The agent asks whether you trust this folder before it starts.",
            "match": [],
            "created_at": now(),
            "answerable": False,
        }
        async with rt.lock_for(session_id):
            current = rt.db.get_session(session_id)
            if current is None or current["state_source"] == "hook" or current["pending_json"]:
                return
            rt.db.update_session(
                session_id,
                state="needs_input",
                state_source="screen",
                state_at=now(),
                pending_json=dumps(pending),
            )
        activity.record(
            rt.db,
            kind="needs_input",
            title="Folder trust needed",
            body="Answer the trust question in the terminal to start the agent.",
            session_id=session_id,
            env=row["env"],
            dedupe_key=f"trust:{session_id}",
            bus=rt.bus,
        )
        rt.bus.publish("sessions")
        break
    if pending is None:
        return

    # Phase 2: clear the request once the dialog is gone. Claude's SessionStart
    # hook usually clears it first; Codex sends no event until a turn ends.
    answer_deadline = loop.time() + answer_timeout
    while loop.time() < answer_deadline:
        await asyncio.sleep(interval)
        row = rt.db.get_session(session_id)
        if row is None or row["status"] != "live":
            return
        current_pending = loads(row["pending_json"])
        if not isinstance(current_pending, dict) or current_pending.get("id") != pending["id"]:
            return  # an agent event replaced or cleared it
        capture = await _capture(rt, row, row["pane_id"], row["socket_path"]) if row["pane_id"] else None
        if capture is None or screen.trust_dialog_visible(capture):
            continue
        async with rt.lock_for(session_id):
            current = rt.db.get_session(session_id)
            latest = loads(current["pending_json"]) if current is not None else None
            if not isinstance(latest, dict) or latest.get("id") != pending["id"]:
                return
            fields: dict[str, Any] = {"pending_json": None}
            if current["state_source"] == "screen":
                fields.update(state="unknown", state_source="terminal", state_at=now())
            rt.db.update_session(session_id, **fields)
        rt.bus.publish("sessions")
        return


async def _report_launch_failure(rt: Runtime, row: sqlite3.Row, failure: tuple[str, int, str]) -> None:
    """Mark a dashboard agent that exited at start, and tell the user why.

    The tmux session stays open with a shell (see ``_AGENT_LAUNCH_SCRIPT``), so
    the full error is in the terminal. The activity item carries the error line.
    """
    program, code, reason = failure
    session_id = row["id"]
    async with rt.lock_for(session_id):
        current = rt.db.get_session(session_id)
        if current is None or current["status"] != "live":
            return
        rt.db.update_session(session_id, state="exited", state_source="terminal", state_at=now())
    activity.record(
        rt.db,
        kind="error",
        title=f"{row['display_name']} could not start",
        body=reason or f"{program} exited with code {code}.",
        session_id=session_id,
        env=row["env"],
        attention=True,
        dedupe_key=f"launch-failed:{session_id}",
        bus=rt.bus,
    )
    rt.bus.publish("sessions")


def _meta(data: dict[str, Any]) -> str | None:
    from .db import dumps

    clean = {k: v for k, v in data.items() if v is not None}
    return dumps(clean) if clean else None


async def end_session(rt: Runtime, session_id: str, *, tmux_session_id: str, tmux_created: int, pane_count: int) -> dict[str, Any]:
    async with rt.lock_for(session_id):
        row = get_row_or_404(rt, session_id)
        if row["status"] != "live":
            if row["status"] in ("ended", "interrupted"):
                return {"ended": True, "already": True}
            raise ApiError(409, "not_live", "The session is not live")

        # Re-validate identity right before the kill.
        socket = row["socket_path"]
        sess_id = row["tmux_session_id"]
        if sess_id != tmux_session_id or row["tmux_created"] != tmux_created:
            raise ApiError(409, "scope_changed", "The session changed since you selected it", pane_count=row["pane_count"])

        # Re-check the live pane count from a fresh snapshot.
        current = await _current_pane_count(rt, row)
        if current is not None and current != pane_count:
            raise ApiError(409, "scope_changed", "The pane count changed", pane_count=current)

        try:
            await rt.tmux_for(row["env"]).kill_session(socket, sess_id)
        except Exception as exc:  # noqa: BLE001
            raise ApiError(502, "env_command_failed", f"Could not end the session: {exc}") from exc

        rt.db.update_session(
            session_id,
            status="ended",
            state="exited",
            end_reason="ended_by_user",
            ended_at=now(),
            pending_json=None,
        )

    activity.record(
        rt.db,
        kind="session_ended",
        title=f"{row['display_name']} ended",
        session_id=session_id,
        env=row["env"],
        attention=False,
        dedupe_key=f"ended:{session_id}:ended_by_user",
        bus=rt.bus,
    )
    rt.bus.publish("sessions")
    # Refresh the cached snapshot so the ended tmux session's name is free at once
    # (for example for an immediate Resume or a new session with the same name).
    try:
        await inventory.refresh_env(rt, row["env"])
    except Exception:  # noqa: BLE001 - the next poll refreshes anyway
        pass
    return {"ended": True, "already": False}


async def _current_pane_count(rt: Runtime, row: sqlite3.Row) -> int | None:
    """Fresh pane count for the row's tmux session, or None if unavailable."""
    try:
        snap = await rt.tmux_for(row["env"]).snapshot()
    except Exception:  # noqa: BLE001
        return None
    tsess = snap.find(row["socket_path"], row["tmux_session_id"])
    if tsess is None:
        return None
    return len(tsess.panes)


async def send_prompt(rt: Runtime, session_id: str, text: str) -> dict[str, Any]:
    text = text or ""
    if not text.strip() or len(text) > MAX_PROMPT:
        raise ApiError(400, "invalid_prompt", "The prompt is empty or too long")

    async with rt.lock_for(session_id):
        row = get_row_or_404(rt, session_id)
        kind = row["kind"]
        if kind not in _AGENT_KINDS:
            raise ApiError(422, "unsupported", "Prompts are only supported for agent sessions")
        if row["status"] != "live":
            raise ApiError(409, "not_live", "The session is not live")

        # Refuse if a structured approval is pending.
        if row["pending_json"]:
            raise ApiError(409, "approval_pending", "Answer the pending approval first")

        pane_id = row["pane_id"]
        socket = row["socket_path"]
        if not pane_id:
            raise ApiError(409, "agent_not_running", "The agent pane is unknown")

        capture = await _capture(rt, row, pane_id, socket)
        if capture is not None and screen.permission_dialog_visible(capture):
            raise ApiError(409, "approval_pending", "Answer the pending approval first")

        # Refuse if the target pane shows a shell (the agent exited).
        if not _agent_foreground(row, kind):
            raise ApiError(409, "agent_not_running", "The agent is not running in its pane")

        try:
            await rt.tmux_for(row["env"]).paste_text(socket, pane_id, text, submit=True)
        except Exception as exc:  # noqa: BLE001
            raise ApiError(502, "env_command_failed", f"Could not send the prompt: {exc}") from exc

        if kind == "codex" and row["state_source"] in codex_runtime.EVENT_SOURCES:
            # Show the turn as running until its start or completion event
            # arrives (an older event cannot undo this; a later one wins).
            rt.db.update_session(session_id, state="working", state_at=now())
            rt.bus.publish("sessions")

    return {"sent": True}


async def _capture(rt: Runtime, row: sqlite3.Row, pane_id: str, socket: str) -> str | None:
    try:
        return await rt.tmux_for(row["env"]).capture(socket, pane_id, lines=80)
    except Exception:  # noqa: BLE001
        return None


async def interrupt(rt: Runtime, session_id: str) -> dict[str, Any]:
    async with rt.lock_for(session_id):
        row = get_row_or_404(rt, session_id)
        kind = row["kind"]
        if kind not in _AGENT_KINDS:
            raise ApiError(422, "unsupported", "Interrupt is only supported for agent sessions")
        if row["status"] != "live":
            raise ApiError(409, "not_live", "The session is not live")
        if not _agent_foreground(row, kind):
            raise ApiError(409, "agent_not_running", "The agent is not running in its pane")
        pane_id = row["pane_id"]
        socket = row["socket_path"]
        if not pane_id:
            raise ApiError(409, "agent_not_running", "The agent pane is unknown")
        try:
            await rt.tmux_for(row["env"]).send_keys(socket, pane_id, "Escape")
        except Exception as exc:  # noqa: BLE001
            raise ApiError(502, "env_command_failed", f"Could not interrupt the session: {exc}") from exc
        # Neither agent reports a user interrupt (Claude's Stop hook skips it and
        # Codex sends no turn-complete), and Escape also dismisses an approval
        # prompt. The agent now waits for input.
        if row["state"] in ("working", "needs_input"):
            rt.db.update_session(session_id, state="idle", state_at=now(), pending_json=None)
            rt.bus.publish("sessions")
    return {"interrupted": True}


async def resume_session(rt: Runtime, session_id: str) -> dict[str, Any]:
    row = get_row_or_404(rt, session_id)
    kind = row["kind"]
    if row["status"] == "live" or kind not in _AGENT_KINDS or not row["agent_session_id"]:
        raise ApiError(409, "not_resumable", "This session cannot be resumed")

    env_id = row["env"]
    env = require_env_up(rt, env_id)
    if not inventory.agents_for(rt, env_id).get(kind, False):
        raise ApiError(400, "kind_unavailable", f"{kind} is not installed in {env_id}")

    agent_session_id = row["agent_session_id"]
    name = row["display_name"]
    project_dir = row["project_dir"] or env.projects_root

    codex_notify = None
    if kind == "codex":
        try:
            from .provision import codex_notify_override as codex_notify
        except Exception:  # noqa: BLE001
            raise ApiError(503, "env_unavailable", "Codex support is not provisioned") from None

    async with rt.lock_for("env:" + env_id):
        if name.casefold() in _live_names(rt, env_id):
            raise ApiError(409, "duplicate_name", f"A live session called {name} already exists")
        relay_id = new_id()
        if kind == "claude":
            agent_argv = ["claude", "--resume", agent_session_id, "--name", name, "--settings", env.claude_settings]
        else:
            notify_override = codex_notify(env, relay_id) if codex_notify is not None else ""
            agent_argv = ["codex", "resume", agent_session_id, "-c", notify_override]
        socket = env.tmux_sockets[0]
        rt.db.insert_session(
            {
                "id": relay_id,
                "env": env_id,
                "kind": kind,
                "display_name": name,
                "tmux_name": name,
                "origin": "dashboard",
                "status": "live",
                "state": "unknown",
                "state_source": None,
                "socket_path": socket,
                "project_dir": project_dir,
                "agent_session_id": agent_session_id,
                "meta_json": _meta({"resumed_from": session_id}),
            }
        )
        try:
            tsid, pane_id, created, server_pid = await rt.tmux_for(env_id).new_session(
                socket,
                name=name,
                cwd=project_dir,
                argv=_launch_wrapper(agent_argv),
                env_vars={"RELAY_SESSION_ID": relay_id, "RELAY_ENV": env_id},
                relay_id=relay_id,
            )
        except Exception as exc:  # noqa: BLE001
            rt.db.update_session(relay_id, status="ended", state="exited", end_reason="closed", ended_at=now())
            raise ApiError(502, "env_command_failed", f"Could not resume: {exc}") from exc
        rt.db.update_session(relay_id, tmux_session_id=tsid, pane_id=pane_id, tmux_created=created, server_pid=server_pid)

    activity.record(
        rt.db,
        kind="session_created",
        title=f"{name} resumed",
        session_id=relay_id,
        env=env_id,
        attention=False,
        dedupe_key=f"created:{relay_id}",
        bus=rt.bus,
    )
    await inventory.refresh_env(rt, env_id)
    start_startup_watch(rt, relay_id)
    return session_view(rt, get_row_or_404(rt, relay_id))
