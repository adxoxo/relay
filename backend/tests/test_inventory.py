"""Tests for relay.inventory.

A small in-memory tmux simulator answers the podman ``exec`` calls that the
foundation ``tmux`` helpers make. The simulator understands the snapshot
script, new-session, set-option, kill-session, has-session, capture-pane,
send-keys, the paste script, realpath, and ``command -v`` probes.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fakes import FakePodman, ok, fail  # noqa: E402

from relay import inventory  # noqa: E402
from relay.podman import ContainerInfo, ProcResult  # noqa: E402
from relay.tmux import DELIM, SESSION_FIELDS, PANE_FIELDS  # noqa: E402


# -- tmux simulator ----------------------------------------------------------


@dataclass
class SimPane:
    pane_id: str
    pid: int
    command: str = "bash"
    active: bool = True
    window_active: bool = True
    window_id: str = "@0"
    activity: int = 100


@dataclass
class SimSession:
    name: str
    id: str
    created: int
    server_pid: int
    relay_id: str | None = None
    panes: list[SimPane] = field(default_factory=list)


@dataclass
class SimServer:
    """One tmux server on one socket, with its sessions and process table."""

    socket: str
    server_pid: int
    sessions: list[SimSession] = field(default_factory=list)
    procs: list[tuple[int, int, str]] = field(default_factory=list)  # pid, ppid, args
    agents: tuple[str, ...] = ("claude", "codex")
    projects: tuple[str, ...] = ("app", "site")

    def find(self, sid: str) -> SimSession | None:
        return next((s for s in self.sessions if s.id == sid), None)


class TmuxSim:
    """Holds one SimServer per (container, socket) and answers exec calls."""

    def __init__(self) -> None:
        self.servers: dict[tuple[str, str], SimServer] = {}
        self.killed: list[tuple[str, str]] = []
        self.sent_keys: list[tuple[str, str, tuple[str, ...]]] = []
        self.pasted: list[tuple[str, str, bytes]] = []
        self.captures: dict[tuple[str, str], str] = {}  # (socket, pane_id) -> screen text
        self._next_sid = 90
        self._next_pane = 90

    def server(self, container: str, socket: str) -> SimServer:
        key = (container, socket)
        srv = self.servers.get(key)
        if srv is None:
            srv = SimServer(socket=socket, server_pid=1000 + len(self.servers))
            self.servers[key] = srv
        return srv

    def add_session(
        self,
        container: str,
        socket: str,
        name: str,
        *,
        command: str = "bash",
        relay_id: str | None = None,
        created: int = 100,
        panes: int = 1,
        procs: list[tuple[int, int, str]] | None = None,
    ) -> SimSession:
        srv = self.server(container, socket)
        sid = f"${self._next_sid}"
        self._next_sid += 1
        sess = SimSession(name=name, id=sid, created=created, server_pid=srv.server_pid, relay_id=relay_id)
        base_pid = 5000 + self._next_pane
        for i in range(panes):
            pane = SimPane(
                pane_id=f"%{self._next_pane}",
                pid=base_pid + i,
                command=command,
                active=(i == 0),
                window_active=(i == 0),
                window_id=f"@{i}",
            )
            self._next_pane += 1
            sess.panes.append(pane)
        srv.sessions.append(sess)
        if procs:
            srv.procs.extend(procs)
        else:
            # A shell process per pane; agent command child if command is an agent.
            for pane in sess.panes:
                srv.procs.append((pane.pid, 1, "-bash"))
                if command in ("claude", "codex"):
                    srv.procs.append((pane.pid + 10000, pane.pid, f"{command} --x"))
        return sess

    # -- exec handling -------------------------------------------------------

    def handler(self, container_of: dict[str, str]):
        """Build a FakePodman handler. ``container_of`` maps env->container.

        The handler inspects the ``exec`` argv (after container name) and
        answers the tmux and shell probes.
        """

        def _container(args: list[str]) -> str:
            # args: ["exec", "--user", U, ...flags..., CONTAINER, *cmd]
            i = 1
            while i < len(args):
                a = args[i]
                if a in ("--user", "-w", "-e"):
                    i += 2
                    continue
                if a in ("-i", "-t"):
                    i += 1
                    continue
                break
            return args[i]

        def _cmd(args: list[str]) -> list[str]:
            i = 1
            while i < len(args):
                a = args[i]
                if a in ("--user", "-w", "-e"):
                    i += 2
                    continue
                if a in ("-i", "-t"):
                    i += 1
                    continue
                break
            return args[i + 1 :]

        def handle(args: list[str], stdin: bytes | None) -> ProcResult:
            if not args or args[0] != "exec":
                return ok()
            container = _container(args)
            cmd = _cmd(args)
            return self._dispatch(container, cmd, stdin)

        return handle

    def _dispatch(self, container: str, cmd: list[str], stdin: bytes | None) -> ProcResult:
        if cmd[:1] == ["sh"] and len(cmd) >= 3 and cmd[1] == "-c":
            body = cmd[2]
            tag = cmd[3] if len(cmd) > 3 else ""
            rest = cmd[4:]
            if tag == "relay-snapshot":
                return self._snapshot(container, rest)
            if tag == "relay-paste":
                return self._paste(container, rest, stdin)
            if tag == "relay-project":
                return self._realpath(container, rest)
            if tag == "relay-projects":
                return self._list_projects(container, rest)
            if "command -v" in body:
                return self._command_v(container)
            return ok()
        if cmd[:1] == ["tmux"]:
            return self._tmux(container, cmd)
        return ok()

    def _command_v(self, container: str) -> ProcResult:
        # Use any server's declared agents for this container, else both.
        agents: tuple[str, ...] = ("claude", "codex")
        for (c, _s), srv in self.servers.items():
            if c == container:
                agents = srv.agents
                break
        return ok("\n".join(agents) + ("\n" if agents else ""))

    def _snapshot(self, container: str, rest: list[str]) -> ProcResult:
        # rest = [SESSION_FORMAT, PANE_FORMAT, *sockets]
        session_fmt, pane_fmt, *sockets = rest
        lines: list[str] = []
        all_procs: dict[int, tuple[int, int, str]] = {}
        for socket in sockets:
            lines.append(f"@@SOCKET\t{socket}")
            srv = self.servers.get((container, socket))
            if srv is None:
                lines.append("@@NOSOCKET")
                continue
            lines.append("@@SESSIONS")
            for s in srv.sessions:
                lines.append(self._session_line(s))
            lines.append("@@PANES")
            for s in srv.sessions:
                for p in s.panes:
                    lines.append(self._pane_line(s, p))
            for pid, ppid, args in srv.procs:
                all_procs[pid] = (pid, ppid, args)
        lines.append("@@PS")
        for pid, ppid, args in all_procs.values():
            lines.append(f"{pid} {ppid} {args}")
        lines.append("@@END")
        return ok("\n".join(lines) + "\n")

    def _session_line(self, s: SimSession) -> str:
        # SESSION_FIELDS order: pid, session_id, created, windows, attached,
        # activity, @relay_id, name
        values = [
            str(s.server_pid),
            s.id,
            str(s.created),
            "1",
            "0",
            "100",
            s.relay_id or "",
            s.name,
        ]
        assert len(values) == len(SESSION_FIELDS)
        return DELIM.join(values)

    def _pane_line(self, s: SimSession, p: SimPane) -> str:
        # PANE_FIELDS: session_id, window_id, window_index, window_active,
        # pane_id, pane_index, pane_active, pane_pid, pane_dead, width, height,
        # window_activity, pane_current_command, pane_current_path
        values = [
            s.id,
            p.window_id,
            "0",
            "1" if p.window_active else "0",
            p.pane_id,
            "0",
            "1" if p.active else "0",
            str(p.pid),
            "0",
            "80",
            "24",
            str(p.activity),
            p.command,
            "/home/node/projects",
        ]
        assert len(values) == len(PANE_FIELDS)
        return DELIM.join(values)

    def _tmux(self, container: str, cmd: list[str]) -> ProcResult:
        # cmd = ["tmux", "-S", socket, subcommand, ...] (possibly "-u" before -S)
        i = 1
        if cmd[i] == "-u":
            i += 1
        assert cmd[i] == "-S"
        socket = cmd[i + 1]
        sub = cmd[i + 2]
        args = cmd[i + 3 :]
        srv = self.servers.get((container, socket))
        if sub == "new-session":
            return self._new_session(container, socket, args)
        if sub == "set-option":
            return self._set_option(srv, args)
        if sub == "has-session":
            sid = _opt(args, "-t")
            return ok() if srv and srv.find(sid) else fail(1, "no session")
        if sub == "kill-session":
            sid = _opt(args, "-t")
            if srv and srv.find(sid):
                srv.sessions = [s for s in srv.sessions if s.id != sid]
                self.killed.append((socket, sid))
                return ok()
            return fail(1, "no session")
        if sub == "capture-pane":
            pane_id = _opt(args, "-t")
            return ok(self.captures.get((socket, pane_id), ""))
        if sub == "send-keys":
            pane_id = _opt(args, "-t")
            keys = tuple(a for a in args if a != "-t" and a != pane_id)
            self.sent_keys.append((socket, pane_id, keys))
            return ok()
        return ok()

    def _new_session(self, container: str, socket: str, args: list[str]) -> ProcResult:
        name = _opt(args, "-s")
        srv = self.server(container, socket)
        # Detect the agent from the wrapped argv after "--".
        command = "bash"
        if "--" in args:
            tail = args[args.index("--") + 1 :]
            joined = " ".join(tail)
            if "claude" in joined:
                command = "claude"
            elif "codex" in joined:
                command = "codex"
        sess = self.add_session(container, socket, name, command=command)
        pane = sess.panes[0]
        # new-session -P -F output: pid|~|session_id|~|created|~|pane_id
        line = DELIM.join([str(srv.server_pid), sess.id, str(sess.created), pane.pane_id])
        # stash so set-option can attach @relay_id
        self._last_new = sess
        return ok(line + "\n")

    def _set_option(self, srv, args: list[str]) -> ProcResult:
        sid = _opt(args, "-t")
        # args: -t <sid> <name> <value>
        if len(args) >= 4 and args[2] == "@relay_id":
            if srv:
                s = srv.find(sid)
                if s:
                    s.relay_id = args[3]
        return ok()

    def _realpath(self, container: str, rest: list[str]) -> ProcResult:
        # rest = [candidate]; simulate realpath -e + directory check.
        candidate = rest[0]
        root = "/home/node/projects"
        if candidate == root:
            return ok(root)
        if candidate.startswith(root + "/"):
            name = candidate[len(root) + 1 :]
            for (c, _s), srv in self.servers.items():
                if c == container and name in srv.projects:
                    return ok(candidate)
            # default: allow known names
            if name in ("app", "site"):
                return ok(candidate)
        return fail(3, "no such file")

    def _list_projects(self, container: str, rest: list[str]) -> ProcResult:
        projects: tuple[str, ...] = ("app", "site")
        for (c, _s), srv in self.servers.items():
            if c == container:
                projects = srv.projects
                break
        return ok("\n".join(projects) + "\n")

    def _paste(self, container: str, rest: list[str], stdin: bytes | None) -> ProcResult:
        # rest = [buffer, socket, pane_id, submit]
        _buf, socket, pane_id, _submit = rest[:4]
        self.pasted.append((socket, pane_id, stdin or b""))
        return ok()


def _opt(args: list[str], flag: str) -> str:
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
    return ""


# -- fixtures ----------------------------------------------------------------


@pytest.fixture
def sim() -> TmuxSim:
    return TmuxSim()


def wire(rt, sim: TmuxSim) -> None:
    container_of = {env_id: env.container for env_id, env in rt.settings.envs.items()}
    rt.podman.handler = sim.handler(container_of)


def socket_of(rt, env_id: str) -> str:
    return rt.settings.env(env_id).tmux_sockets[0]


# -- tests -------------------------------------------------------------------


async def test_same_names_map_to_distinct_rows(rt, sim):
    s1 = socket_of(rt, "work1")
    s2 = socket_of(rt, "work2")
    sim.add_session("job-work1", s1, "agent1", command="claude")
    sim.add_session("job-work2", s2, "agent1", command="codex")
    wire(rt, sim)

    await inventory.refresh_env(rt, "work1")
    await inventory.refresh_env(rt, "work2")

    rows = rt.db.list_sessions()
    assert len(rows) == 2
    envs = {r["env"] for r in rows}
    assert envs == {"work1", "work2"}
    ids = {r["id"] for r in rows}
    assert len(ids) == 2
    kinds = {r["env"]: r["kind"] for r in rows}
    assert kinds == {"work1": "claude", "work2": "codex"}


async def test_name_reuse_creates_new_row(rt, sim):
    s1 = socket_of(rt, "work1")
    sim.add_session("job-work1", s1, "agent1", command="claude")
    wire(rt, sim)
    await inventory.refresh_env(rt, "work1")
    first = rt.db.list_sessions()[0]

    # The tmux session ends, a new one reuses the name with a new id/created.
    srv = sim.servers[("job-work1", s1)]
    srv.sessions.clear()
    sim.add_session("job-work1", s1, "agent1", command="claude", created=200)
    await inventory.refresh_env(rt, "work1")

    rows = rt.db.list_sessions()
    assert len(rows) == 2
    live = [r for r in rows if r["status"] == "live"]
    ended = [r for r in rows if r["status"] != "live"]
    assert len(live) == 1 and len(ended) == 1
    assert live[0]["id"] != first["id"]
    assert ended[0]["id"] == first["id"]
    assert ended[0]["status"] == "ended" and ended[0]["end_reason"] == "closed"


async def test_container_restart_marks_interrupted_with_resume(rt, sim):
    s1 = socket_of(rt, "work1")
    sim.add_session("job-work1", s1, "agent1", command="claude")
    wire(rt, sim)
    # Dashboard-launched agent row (origin dashboard, has agent_session_id).
    await inventory.refresh_env(rt, "work1")
    row = rt.db.list_sessions()[0]
    rt.db.update_session(row["id"], origin="dashboard", agent_session_id="00000000-0000-0000-0000-000000000001")

    # Container restarts: new started_at, and its tmux server lost the session.
    rt.podman.containers["job-work1"] = ContainerInfo(
        name="job-work1", exists=True, running=True, status="running",
        id="id-job-work1", started_at="2030-01-01T00:00:00+08:00", network_mode="slirp4netns",
    )
    srv = sim.servers[("job-work1", s1)]
    srv.sessions.clear()
    await inventory.refresh_env(rt, "work1")

    updated = rt.db.get_session(row["id"])
    assert updated["status"] == "interrupted"
    assert updated["end_reason"] == "container_restarted"

    from relay import sessions as sess_mod
    view = sess_mod.session_view(rt, updated)
    assert view["capabilities"]["resume"] is True


async def test_container_stopped_marks_interrupted(rt, sim):
    s1 = socket_of(rt, "work1")
    sim.add_session("job-work1", s1, "agent1", command="claude")
    wire(rt, sim)
    await inventory.refresh_env(rt, "work1")
    row = rt.db.list_sessions()[0]

    rt.podman.containers["job-work1"] = ContainerInfo(
        name="job-work1", exists=True, running=False, status="exited", id="id-job-work1",
    )
    await inventory.refresh_env(rt, "work1")
    updated = rt.db.get_session(row["id"])
    assert updated["status"] == "interrupted"
    assert updated["end_reason"] == "container_stopped"


async def test_server_restart_marks_interrupted(rt, sim):
    s1 = socket_of(rt, "work1")
    sim.add_session("job-work1", s1, "agent1", command="claude")
    wire(rt, sim)
    await inventory.refresh_env(rt, "work1")
    row = rt.db.list_sessions()[0]

    # Same container, but the tmux server restarted (new server pid) and the
    # session is gone.
    srv = sim.servers[("job-work1", s1)]
    srv.sessions.clear()
    srv.server_pid = 9999
    await inventory.refresh_env(rt, "work1")
    updated = rt.db.get_session(row["id"])
    assert updated["status"] == "interrupted"
    assert updated["end_reason"] == "server_restarted"


async def test_discovered_kind_and_activity(rt, sim):
    s2 = socket_of(rt, "work2")
    sim.add_session("job-work2", s2, "agent1", command="codex")
    sim.add_session("job-work2", s2, "shellone", command="bash")
    wire(rt, sim)
    await inventory.refresh_env(rt, "work2")

    rows = {r["display_name"]: r for r in rt.db.list_sessions()}
    assert rows["agent1"]["kind"] == "codex"
    assert rows["agent1"]["origin"] == "discovered"
    assert rows["shellone"]["kind"] == "shell"

    items = {i["title"] for i in __import__("relay.activity", fromlist=["list_items"]).list_items(rt.db)}
    assert any("agent1" in t for t in items)


async def test_snapshot_failure_keeps_rows(rt, sim):
    s1 = socket_of(rt, "work1")
    sim.add_session("job-work1", s1, "agent1", command="claude")
    wire(rt, sim)
    await inventory.refresh_env(rt, "work1")
    assert len(rt.db.list_sessions()) == 1

    def broken(args, stdin):
        if args and args[0] == "exec" and any("relay-snapshot" in a for a in args):
            return fail(1, "boom")
        return ok()

    rt.podman.handler = broken
    await inventory.refresh_env(rt, "work1")
    rows = rt.db.list_sessions()
    assert len(rows) == 1 and rows[0]["status"] == "live"
    assert rt.env_status["work1"].error is not None


async def test_agents_cached_per_container_run(rt, sim):
    s1 = socket_of(rt, "work1")
    sim.server("job-work1", s1).agents = ("claude",)
    wire(rt, sim)
    await inventory.refresh_env(rt, "work1")
    assert inventory.agents_for(rt, "work1") == {"claude": True, "codex": False}


async def test_agent_probe_output_counts_even_when_exit_status_fails(rt):
    # work1 has claude but no codex: the probe loop ends with a failed
    # ``command -v codex``, so the shell reports status 127 with "claude" on stdout.
    rt.podman.handler = lambda args, _input: ProcResult(127, b"claude\n", b"")
    found = await inventory.detect_agents(rt, "work1", "run-1")
    assert found == {"claude": True, "codex": False}


async def test_codex_node_foreground_counts_as_agent_running(rt, sim):
    # Real work2 shape: tmux reports ``node`` as the foreground command and the
    # process tree is ``-bash`` -> ``node /home/node/.local/bin/codex``.
    from relay import sessions

    s2 = socket_of(rt, "work2")
    sess = sim.add_session(
        "job-work2", s2, "agent1", command="node",
        procs=[(7001, 1, "-bash"), (7002, 7001, "node /home/node/.local/bin/codex")],
    )
    sess.panes[0].pid = 7001
    wire(rt, sim)

    await inventory.refresh_env(rt, "work2")

    row = rt.db.list_sessions(env="work2")[0]
    assert row["kind"] == "codex"
    assert row["foreground"] == "codex"
    caps = sessions.session_view(rt, row)["capabilities"]
    assert caps["prompt"] and caps["interrupt"]


async def test_idle_poll_publishes_nothing(rt, sim):
    # Each bus message makes every open browser refetch /api/state. A poll that
    # changes nothing must stay silent; a real change must still be published.
    s2 = socket_of(rt, "work2")
    sim.add_session("job-work2", s2, "agent3", command="bash")
    wire(rt, sim)
    await inventory.refresh_env(rt, "work2")

    seen: list[str] = []
    with rt.bus.subscribe() as queue:
        await inventory.refresh_env(rt, "work2")
        while not queue.empty():
            seen.append(queue.get_nowait())
        assert seen == []

        sim.add_session("job-work2", s2, "agent4", command="bash")
        await inventory.refresh_env(rt, "work2")
        while not queue.empty():
            seen.append(queue.get_nowait())
    assert "sessions" in seen and "envs" in seen


# -- snapshot races (found live on 2026-10-01) -----------------------------------


def _dashboard_row(rt, name, **extra):
    fields = dict(env="work1", kind="shell", display_name=name, tmux_name=name, origin="dashboard",
                  status="live", state="unknown", socket_path=socket_of(rt, "work1"))
    fields.update(extra)
    return rt.db.insert_session(fields)


async def test_create_in_progress_is_not_ended_or_duplicated(rt, sim):
    # create_session inserted the row and ran new-session, but has not set
    # @relay_id or stored the tmux ids yet when the poll snapshot runs.
    s1 = socket_of(rt, "work1")
    wire(rt, sim)
    rid = _dashboard_row(rt, "agent9")
    sim.add_session("job-work1", s1, "agent9")
    await inventory.refresh_env(rt, "work1")
    rows = rt.db.list_sessions()
    assert len(rows) == 1
    assert rows[0]["id"] == rid and rows[0]["status"] == "live"


async def test_wrongly_ended_row_is_revived_without_churn(rt, sim):
    # A row that the merge ended by mistake while its tmux session still runs
    # (the session carries the row's @relay_id). It used to make a new
    # discovered row every poll, forever.
    s1 = socket_of(rt, "work1")
    wire(rt, sim)
    rid = _dashboard_row(rt, "agent9", tmux_session_id="$50", tmux_created=100)
    rt.db.update_session(rid, status="ended", state="exited", end_reason="closed", ended_at=1.0)
    sim.add_session("job-work1", s1, "agent9", relay_id=rid)
    for _ in range(3):
        await inventory.refresh_env(rt, "work1")
    rows = rt.db.list_sessions()
    assert [r["id"] for r in rows] == [rid]
    assert rows[0]["status"] == "live" and rows[0]["end_reason"] is None


async def test_user_ended_session_is_not_rediscovered_while_the_kill_settles(rt, sim):
    s1 = socket_of(rt, "work1")
    wire(rt, sim)
    sess = sim.add_session("job-work1", s1, "scratch")
    server_pid = sim.servers[("job-work1", s1)].server_pid
    rid = _dashboard_row(rt, "scratch", origin="discovered", tmux_session_id=sess.id,
                         tmux_created=sess.created, server_pid=server_pid)
    rt.db.update_session(rid, status="ended", state="exited", end_reason="ended_by_user", ended_at=1.0)
    await inventory.refresh_env(rt, "work1")
    rows = rt.db.list_sessions()
    assert [r["id"] for r in rows] == [rid]
    assert rows[0]["status"] == "ended"


async def test_a_new_hand_started_session_is_still_discovered(rt, sim):
    s1 = socket_of(rt, "work1")
    wire(rt, sim)
    sim.add_session("job-work1", s1, "laptop-shell")
    await inventory.refresh_env(rt, "work1")
    rows = rt.db.list_sessions()
    assert len(rows) == 1 and rows[0]["origin"] == "discovered" and rows[0]["status"] == "live"
