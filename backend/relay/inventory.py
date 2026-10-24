"""Session inventory: reconcile live tmux state into the ``sessions`` table.

Live tmux inventory is the source of truth for whether a session exists. This
module polls each environment, snapshots its tmux sockets, and merges the
snapshot into the database by the rules in ARCHITECTURE.md section 5:

- Match a stored row to a tmux session by ``@relay_id`` first, then by the
  identity tuple ``(env, container_started_at, socket, server_pid, session_id,
  tmux_created)``.
- Unmatched tmux sessions become new discovered rows.
- Unmatched live rows end with the correct status and end reason.

A new tmux session that reuses an old name becomes a new row, because names
are not part of the identity.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any

from . import activity, codex_runtime, screen
from .db import Database, new_id, now
from .podman import ContainerInfo
from .runtime import Runtime
from .tmux import Snapshot, TmuxPane, TmuxSession, detect_kind

log = logging.getLogger("relay.inventory")

# States that inventory may overwrite (fallback ownership). A hook, notify, or
# codex_rollout source is authoritative and must not be clobbered by terminal
# heuristics.
_FALLBACK_SOURCES = (None, "terminal")

# One reconcile pass per day removes very old ended rows.
_ENDED_RETENTION_DAYS = 30
_AGENT_KINDS = frozenset({"claude", "codex"})


# -- agent CLI detection -----------------------------------------------------

_AGENT_PROBE = 'for b in claude codex; do command -v "$b" >/dev/null 2>&1 && echo "$b"; done; exit 0'


async def detect_agents(rt: Runtime, env_id: str, container_started_at: str | None) -> dict[str, bool]:
    """Return which agent CLIs exist in the container, cached per container run.

    The cache key is the container ``started_at`` so a container restart or a
    freshly installed CLI is picked up on the next poll.
    """
    cache: dict[str, tuple[str | None, dict[str, bool]]] = rt.__dict__.setdefault("_agent_cache", {})
    cached = cache.get(env_id)
    if cached is not None and cached[0] == container_started_at:
        return cached[1]
    env = rt.settings.env(env_id)
    found = {"claude": False, "codex": False}
    try:
        res = await rt.podman.exec(env, ["sh", "-c", _AGENT_PROBE], timeout=15)
    except (OSError, asyncio.TimeoutError):
        return found
    # Parse stdout whatever the exit status is: a missing binary must not hide
    # the ones that exist (the loop's last ``command -v`` sets the status).
    for line in res.out.splitlines():
        name = line.strip()
        if name in found:
            found[name] = True
    cache[env_id] = (container_started_at, found)
    return found


def agents_for(rt: Runtime, env_id: str) -> dict[str, bool]:
    """Last cached agent availability for ``env_id`` (default: unknown -> False)."""
    cache: dict[str, tuple[str | None, dict[str, bool]]] = rt.__dict__.get("_agent_cache", {})
    cached = cache.get(env_id)
    return dict(cached[1]) if cached is not None else {"claude": False, "codex": False}


# -- refresh -----------------------------------------------------------------


async def refresh_env(rt: Runtime, env_id: str) -> None:
    """Inspect one environment, snapshot tmux when up, and reconcile the DB.

    On any container or snapshot failure the stored rows are kept untouched and
    the error string is recorded on ``rt.env_status[env_id]``.
    """
    env = rt.settings.env(env_id)
    status = rt.env_status[env_id]
    before = _env_signature(rt, env_id)
    info: ContainerInfo = await rt.podman.inspect(env)
    status.container = info

    if not info.running:
        status.snapshot = None
        status.error = info.error
        await detect_agents(rt, env_id, info.started_at)
        async with rt.inventory_lock:
            changed = _end_all_live(rt.db, env_id, info, bus=rt.bus)
        _publish(rt, env_id, before, sessions_changed=changed)
        return

    try:
        # Capture agent screens first: the snapshot and the merge below must
        # stay close together, or a session created or ended in between is
        # judged on stale data. Captures use each row's stored pane.
        screens_at = now()
        screens = await _capture_agent_screens(rt, env_id)
        snap_started = now()
        snapshot: Snapshot = await rt.tmux_for(env_id).snapshot()
    except Exception as exc:  # noqa: BLE001 - keep old rows on any snapshot failure
        status.error = f"snapshot failed: {exc}"
        log.warning("snapshot failed for %s: %s", env_id, exc)
        _publish(rt, env_id, before, sessions_changed=False)
        return

    status.snapshot = snapshot
    status.snapshot_at = now()
    status.error = None
    await detect_agents(rt, env_id, info.started_at)

    async with rt.inventory_lock:
        changed = _reconcile(rt.db, rt.bus, env_id, info, snapshot, screens, snap_started=snap_started)
    _publish(rt, env_id, before, sessions_changed=changed)

    # Lifecycle of verified Codex thread links, read from their rollouts at
    # this poll's cadence. It owns state with source 'codex_rollout', which the
    # terminal fallback above never overwrites. The screens add only a visible
    # approval dialog (needs_input) on top of that state.
    try:
        await codex_runtime.poll_env(rt, env_id, snapshot, screens, screens_at)
    except Exception:  # noqa: BLE001 - the lifecycle source must never stop inventory
        log.exception("Codex lifecycle poll failed for %s", env_id)


async def _capture_agent_screens(rt: Runtime, env_id: str) -> dict[tuple[str, str], str]:
    """Capture the stored pane of each live agent row that terminal state owns.

    Returns ``{(socket, pane_id): screen_text}``. A capture failure drops that
    pane only. Rows owned by a hook or notify source are skipped, because their
    state is authoritative and must not be re-derived from the screen. A Codex
    row with a verified thread link is captured too, but only for its approval
    dialog (``codex_runtime._screen_overlay``); the fallback never runs for it.
    """
    rows = [
        r
        for r in rt.db.list_sessions(env=env_id, statuses=("live",))
        if r["kind"] in _AGENT_KINDS
        and r["pane_id"]
        and (
            r["state_source"] in _FALLBACK_SOURCES
            or (
                r["kind"] == "codex"
                and r["state_source"] in codex_runtime.EVENT_SOURCES
                and codex_runtime.is_verified(codex_runtime.binding_of(r))
            )
        )
    ]
    if not rows:
        return {}
    tmux = rt.tmux_for(env_id)

    async def one(row: sqlite3.Row) -> tuple[tuple[str, str], str | None]:
        key = (row["socket_path"], row["pane_id"])
        try:
            return key, await tmux.capture(row["socket_path"], row["pane_id"], lines=80)
        except Exception:  # noqa: BLE001 - a capture failure just leaves state as is
            return key, None

    results = await asyncio.gather(*(one(r) for r in rows))
    return {key: text for key, text in results if text is not None}


def _env_signature(rt: Runtime, env_id: str) -> tuple[Any, ...]:
    """What an EnvView shows, minus the poll timestamp."""
    st = rt.env_status[env_id]
    info = st.container
    return (
        info.running if info else None,
        info.status if info else None,
        info.started_at if info else None,
        info.network_mode if info else None,
        st.error,
        tuple(sorted(agents_for(rt, env_id).items())),
    )


def _publish(rt: Runtime, env_id: str, before: tuple[Any, ...], *, sessions_changed: bool) -> None:
    """Tell browsers what changed. Nothing is sent when nothing changed.

    Each message makes every open browser refetch /api/state, so an idle
    dashboard should stay quiet between polls.
    """
    topics = []
    if sessions_changed:
        topics.append("sessions")
    if sessions_changed or _env_signature(rt, env_id) != before:
        topics.append("envs")
    if topics:
        rt.bus.publish(*topics)


def _end_all_live(db: Database, env_id: str, info: ContainerInfo, *, bus: Any = None) -> bool:
    """Container is down: mark every live row interrupted/container_stopped."""
    changed = False
    for row in db.list_sessions(env=env_id, statuses=("live",)):
        _mark_interrupted(db, row, "container_stopped", bus=bus)
        changed = True
    return changed


def _reconcile(
    db: Database,
    bus: Any,
    env_id: str,
    info: ContainerInfo,
    snap: Snapshot,
    screens: dict[tuple[str, str], str] | None = None,
    *,
    snap_started: float | None = None,
) -> bool:
    """Merge ``snap`` into the sessions table for one running environment.

    ``snap_started`` is when the tmux snapshot began. A row written after it
    (a session created or ended while the snapshot ran) is not judged against
    this snapshot, because the snapshot cannot show it correctly yet.
    """
    screens = screens or {}
    managed = {s.path for s in snap.sockets}
    live_rows = list(db.list_sessions(env=env_id, statuses=("live",)))

    # Only consider tmux sessions on sockets we scan.
    tmux_sessions = [s for s in snap.sessions if s.socket in managed]
    by_relay = {s.relay_id: s for s in tmux_sessions if s.relay_id}

    matched_tmux: set[int] = set()
    changed = False
    deferred_names: set[str] = set()

    # 1. Match each live row to a tmux session.
    for row in live_rows:
        match = _match_row(row, tmux_sessions, by_relay)
        if match is None:
            if _changed_during_snapshot(row, snap_started):
                # A create (or another write) happened while the snapshot ran,
                # so the snapshot cannot show this row yet. The next poll decides.
                deferred_names.add(row["tmux_name"])
                continue
            _end_unmatched(db, bus, row, info, snap)
            changed = True
            continue
        matched_tmux.add(id(match))
        if _update_matched(db, bus, row, match, snap, info, screens):
            changed = True

    # 2. Unmatched tmux sessions become new discovered rows, unless relay
    #    already has a row for them.
    for tsess in tmux_sessions:
        if id(tsess) in matched_tmux or tsess.name in deferred_names:
            continue
        known = _known_row(db, env_id, tsess)
        if known is not None:
            if known["status"] == "live":
                continue  # never make a duplicate of a live row
            if known["end_reason"] == "ended_by_user":
                continue  # relay killed it moments ago; the kill is settling
            # relay ended this row, but its tmux session still runs: it was a
            # mistake (for example a snapshot that missed it). Bring it back.
            _revive(db, bus, known, tsess, snap, info, screens)
            changed = True
            continue
        _discover(db, bus, env_id, info, tsess, snap, screens)
        changed = True

    return changed


# A live row with no tmux id yet is a create in progress. After this long it
# is a leftover (for example relay stopped mid-create) and may end.
_CREATE_GRACE = 60.0


def _changed_during_snapshot(row: sqlite3.Row, snap_started: float | None) -> bool:
    """True when the snapshot cannot be trusted to show this row yet."""
    if row["tmux_session_id"] is None:
        return now() - (row["created_at"] or 0) < _CREATE_GRACE
    if snap_started is None:
        return False
    return (row["updated_at"] or 0) >= snap_started


def _known_row(db: Database, env_id: str, tsess: TmuxSession) -> sqlite3.Row | None:
    """relay's row for a tmux session: by ``@relay_id`` first, else by exact identity."""
    if tsess.relay_id:
        row = db.get_session(tsess.relay_id)
        if row is not None and row["env"] == env_id:
            return row
    rows = db.query(
        "SELECT * FROM sessions WHERE env = ? AND socket_path = ? AND server_pid = ?"
        " AND tmux_session_id = ? AND tmux_created = ? ORDER BY created_at DESC LIMIT 1",
        (env_id, tsess.socket, tsess.server_pid, tsess.id, tsess.created),
    )
    return rows[0] if rows else None


def _revive(
    db: Database,
    bus: Any,
    row: sqlite3.Row,
    tsess: TmuxSession,
    snap: Snapshot,
    info: ContainerInfo,
    screens: dict[tuple[str, str], str],
) -> None:
    """Make an ended row live again, because its tmux session still runs."""
    log.info("reviving %s (%s): its tmux session %s still runs", row["id"], row["display_name"], tsess.id)
    db.update_session(
        row["id"], status="live", ended_at=None, end_reason=None, state="unknown", state_source="terminal"
    )
    fresh = db.get_session(row["id"])
    if fresh is not None:
        _update_matched(db, bus, fresh, tsess, snap, info, screens)


def _match_row(row: sqlite3.Row, tmux_sessions: list[TmuxSession], by_relay: dict[str, TmuxSession]) -> TmuxSession | None:
    # Match by @relay_id first (dashboard sessions), then by identity tuple.
    rid = row["id"]
    m = by_relay.get(rid)
    if m is not None:
        return m
    for tsess in tmux_sessions:
        if (
            tsess.socket == row["socket_path"]
            and tsess.server_pid == row["server_pid"]
            and tsess.id == row["tmux_session_id"]
            and tsess.created == row["tmux_created"]
        ):
            # A tmux session that carries a different relay id is a new row.
            if tsess.relay_id and tsess.relay_id != rid:
                continue
            return tsess
    return None


def _server_pid_for(snap: Snapshot, socket: str) -> int | None:
    for s in snap.sockets:
        if s.path == socket:
            return s.server_pid
    return None


def _end_unmatched(db: Database, bus: Any, row: sqlite3.Row, info: ContainerInfo, snap: Snapshot) -> None:
    """A live row whose tmux session is gone. Choose status and end reason.

    - Container restarted (started_at changed) -> interrupted/container_restarted.
    - Same container, server pid changed or the socket has no server ->
      interrupted/server_restarted.
    - Same server still running -> ended/closed.
    """
    if row["container_started_at"] and info.started_at and row["container_started_at"] != info.started_at:
        _mark_interrupted(db, row, "container_restarted", bus=bus)
        return
    live_pid = _server_pid_for(snap, row["socket_path"])
    if row["server_pid"] is not None and live_pid is not None and live_pid != row["server_pid"]:
        _mark_interrupted(db, row, "server_restarted", bus=bus)
        return
    if row["server_pid"] is not None and live_pid is None:
        # The socket has no server at all: the tmux server is gone.
        _mark_interrupted(db, row, "server_restarted", bus=bus)
        return
    _finish(db, bus, row, "ended", "closed", "exited")


def _mark_interrupted(db: Database, row: sqlite3.Row, reason: str, *, bus: Any = None) -> None:
    _finish(db, bus, row, "interrupted", reason, "exited")


def _finish(db: Database, bus: Any, row: sqlite3.Row, status_val: str, reason: str, end_state: str) -> None:
    ts = now()
    db.update_session(
        row["id"],
        status=status_val,
        state=end_state,
        ended_at=ts,
        end_reason=reason,
        pending_json=None,  # an ended session has no open request
    )
    if status_val == "interrupted":
        activity.record(
            db,
            kind="session_interrupted",
            title=f"{row['display_name']} interrupted",
            session_id=row["id"],
            env=row["env"],
            attention=True,
            dedupe_key=f"interrupted:{row['id']}:{reason}",
            bus=bus,
        )
    else:
        activity.record(
            db,
            kind="session_ended",
            title=f"{row['display_name']} ended",
            session_id=row["id"],
            env=row["env"],
            attention=False,
            dedupe_key=f"ended:{row['id']}:{reason}",
            bus=bus,
        )


def _update_matched(db: Database, bus: Any, row: sqlite3.Row, tsess: TmuxSession, snap: Snapshot, info: ContainerInfo, screens: dict[tuple[str, str], str] | None = None) -> bool:
    """Update a matched live row from its tmux session. Returns True on change."""
    screens = screens or {}
    pane = _target_pane(row, tsess)
    fields: dict[str, Any] = {}

    def setf(col: str, value: Any) -> None:
        if row[col] != value:
            fields[col] = value

    setf("tmux_name", tsess.name)
    setf("container_id", info.id)
    setf("container_started_at", info.started_at)
    setf("server_pid", tsess.server_pid)
    setf("tmux_session_id", tsess.id)
    setf("tmux_created", tsess.created)
    setf("pane_count", len(tsess.panes))

    # Re-detect kind for discovered rows only.
    if row["origin"] == "discovered" and pane is not None:
        kind = detect_kind(snap, pane)
        setf("kind", kind)
    else:
        kind = row["kind"]

    if pane is not None:
        setf("pane_id", pane.pane_id)
        setf("pane_pid", pane.pid)
        setf("foreground", _foreground_label(snap, pane, kind))
        setf("activity_at", float(pane.activity))

    pane_screen = screens.get((tsess.socket, pane.pane_id)) if pane is not None else None
    _apply_fallback_state(row, kind, pane, fields, pane_screen)

    if fields:
        db.update_session(row["id"], **fields)
        return True
    return False


def _foreground_label(snap: Snapshot, pane: TmuxPane, kind: str) -> str:
    """The pane's foreground command, normalised to the agent name when the agent runs.

    Codex is a Node script, so tmux reports ``node`` as the foreground command.
    When the foreground is not a shell and the pane's process tree contains
    this row's agent, report the agent name so capability checks work.
    """
    cmd = pane.current_command
    if kind in _AGENT_KINDS and cmd != kind and not pane.shell_foreground and detect_kind(snap, pane) == kind:
        return kind
    return cmd


def _target_pane(row: sqlite3.Row, tsess: TmuxSession) -> TmuxPane | None:
    """Prefer the stored pane while it still exists, else the tmux target pane."""
    stored = row["pane_id"]
    if stored:
        p = tsess.pane(stored)
        if p is not None:
            return p
    return tsess.target_pane


def _apply_fallback_state(row: sqlite3.Row, kind: str, pane: TmuxPane | None, fields: dict[str, Any], pane_screen: str | None = None) -> None:
    """Set ``state`` only when inventory owns it (see ARCHITECTURE.md section 5)."""
    source = row["state_source"]

    # Agent-exited override: an agent row whose foreground is a shell exited,
    # even if a hook/notify source last wrote the state.
    if kind in _AGENT_KINDS and pane is not None and pane.shell_foreground:
        if row["state"] != "exited" or row["state_source"] != "terminal":
            fields["state"] = "exited"
            fields["state_source"] = "terminal"
            fields["state_at"] = now()
        return

    if source not in _FALLBACK_SOURCES:
        return  # a hook or notify source owns the state

    new_state = _fallback_state(kind, pane, pane_screen)
    if row["state"] != new_state or row["state_source"] != "terminal":
        fields["state"] = new_state
        fields["state_source"] = "terminal"
        fields["state_at"] = now()


def _fallback_state(kind: str, pane: TmuxPane | None, pane_screen: str | None = None) -> str:
    if pane is None:
        return "unknown"
    if kind == "shell":
        return "idle" if pane.shell_foreground else "working"
    if kind in _AGENT_KINDS:
        if pane.shell_foreground:
            return "exited"
        # A running agent (not a shell): read its state from the screen when we
        # have a capture. Without a capture, keep the old "unknown".
        if pane_screen is not None:
            return screen.agent_screen_state(pane_screen, kind)
        return "unknown"
    return "unknown"


def _discover(db: Database, bus: Any, env_id: str, info: ContainerInfo, tsess: TmuxSession, snap: Snapshot, screens: dict[tuple[str, str], str] | None = None) -> None:
    """Insert a new discovered row for a tmux session we do not track yet."""
    screens = screens or {}
    pane = tsess.target_pane
    kind = detect_kind(snap, pane)
    pane_screen = screens.get((tsess.socket, pane.pane_id)) if pane is not None else None
    state = _fallback_state(kind, pane, pane_screen)
    fields: dict[str, Any] = {
        "id": new_id(),
        "env": env_id,
        "kind": kind,
        "display_name": tsess.name,
        "tmux_name": tsess.name,
        "origin": "discovered",
        "status": "live",
        "state": state,
        "state_source": "terminal",
        "state_at": now(),
        "container_id": info.id,
        "container_started_at": info.started_at,
        "socket_path": tsess.socket,
        "server_pid": tsess.server_pid,
        "tmux_session_id": tsess.id,
        "tmux_created": tsess.created,
        "pane_id": pane.pane_id if pane else None,
        "pane_pid": pane.pid if pane else None,
        "pane_count": len(tsess.panes),
        "foreground": _foreground_label(snap, pane, kind) if pane else None,
        "activity_at": float(pane.activity) if pane else None,
        "meta_json": None,
    }
    inserted_id = db.insert_session(fields)
    activity.record(
        db,
        kind="session_created",
        title=f"{tsess.name} discovered",
        session_id=inserted_id,
        env=env_id,
        attention=False,
        dedupe_key=f"created:{inserted_id}",
        bus=bus,
    )


# -- lifecycle loops ---------------------------------------------------------


async def startup_reconcile(rt: Runtime) -> None:
    """First refresh of every environment (handles a laptop reboot)."""
    await asyncio.gather(*(refresh_env(rt, env_id) for env_id in rt.settings.envs), return_exceptions=True)
    _prune_old(rt.db)


async def run_forever(rt: Runtime) -> None:
    """Poll every environment forever. Survive and log every exception."""
    interval = rt.settings.poll_interval
    last_prune = now()
    while True:
        try:
            results = await asyncio.gather(
                *(refresh_env(rt, env_id) for env_id in rt.settings.envs),
                return_exceptions=True,
            )
            for env_id, res in zip(rt.settings.envs, results):
                if isinstance(res, Exception):
                    log.warning("refresh_env(%s) failed: %s", env_id, res)
            if now() - last_prune >= 86400:
                _prune_old(rt.db)
                last_prune = now()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the poll loop must never die
            log.exception("inventory poll failed")
        await asyncio.sleep(interval)


def _prune_old(db: Database) -> None:
    cutoff = now() - _ENDED_RETENTION_DAYS * 86400
    db.execute("DELETE FROM sessions WHERE status = 'ended' AND ended_at IS NOT NULL AND ended_at < ?", (cutoff,))
    activity.prune(db)
