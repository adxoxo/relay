"""Tests for relay.previews.

The FakePodman answers ``exec`` calls. A small stateful tmux model here turns
those calls into snapshot output, so start, stop, readiness, reconciliation,
and logs can be exercised without a real container.
"""

from __future__ import annotations

import asyncio
import socket as socketlib
from contextlib import closing

import pytest

from fakes import FakePodman, ok, fail
from relay import previews
from relay.errors import ApiError
from relay.podman import ContainerInfo
from relay.runtime import Runtime
from relay.tmux import DELIM, PANE_FIELDS, SESSION_FIELDS


# -- a tiny tmux model -------------------------------------------------------

class FakeTmuxState:
    """Holds detached sessions the fake created, keyed by socket."""

    def __init__(self) -> None:
        self.next_session = 1
        self.next_pane = 1
        self.next_created = 1000
        self.server_pid = 4242
        # socket -> {session_id: {name, created, pane_id}}
        self.sessions: dict[str, dict[str, dict]] = {}
        self.captures: dict[str, str] = {}  # pane_id -> text

    def new_session(self, socket: str, name: str) -> tuple[int, str, int, str]:
        sid = f"${self.next_session}"
        pid = f"%{self.next_pane}"
        created = self.next_created
        self.next_session += 1
        self.next_pane += 1
        self.next_created += 1
        self.sessions.setdefault(socket, {})[sid] = {
            "name": name,
            "created": created,
            "pane_id": pid,
        }
        return self.server_pid, sid, created, pid

    def kill(self, socket: str, sid: str) -> None:
        self.sessions.get(socket, {}).pop(sid, None)

    def snapshot_text(self, sockets: list[str]) -> str:
        parts: list[str] = []
        for sock in sockets:
            parts.append(f"@@SOCKET\t{sock}")
            sess = self.sessions.get(sock, {})
            if not sess:
                parts.append("@@NOSERVER")
                continue
            parts.append("@@SESSIONS")
            for sid, info in sess.items():
                fields = [
                    str(self.server_pid),   # pid
                    sid,                    # session_id
                    str(info["created"]),   # session_created
                    "1",                    # windows
                    "0",                    # attached
                    str(info["created"]),   # activity
                    "",                     # @relay_id
                    info["name"],           # session_name
                ]
                assert len(fields) == len(SESSION_FIELDS)
                parts.append(DELIM.join(fields))
            parts.append("@@PANES")
            for sid, info in sess.items():
                pane = [
                    sid,                    # session_id
                    "@1",                   # window_id
                    "0",                    # window_index
                    "1",                    # window_active
                    info["pane_id"],        # pane_id
                    "0",                    # pane_index
                    "1",                    # pane_active
                    "9999",                 # pane_pid
                    "0",                    # pane_dead
                    "200",                  # width
                    "50",                   # height
                    str(info["created"]),   # window_activity
                    "node",                 # pane_current_command
                    "/home/node/projects",  # pane_current_path
                ]
                assert len(pane) == len(PANE_FIELDS)
                parts.append(DELIM.join(pane))
        parts.append("@@PS")
        parts.append("@@END")
        return "\n".join(parts) + "\n"


def make_handler(state: FakeTmuxState, *, project_root: str = "/home/node/projects"):
    """Return a FakePodman handler that speaks enough tmux for the previews module."""

    def handler(args: list[str], input_bytes):
        cmd = _in_container_cmd(args)
        if not cmd:
            return ok()
        # realpath -e -- <path>
        if cmd[:3] == ["realpath", "-e", "--"]:
            path = cmd[3]
            if path == project_root or path.startswith(project_root + "/"):
                return ok(path + "\n")
            return fail(1, "realpath: No such file")
        if cmd[:2] == ["test", "-d"]:
            return ok()
        # snapshot: sh -c SCRIPT relay-snapshot SESSION_FMT PANE_FMT sock...
        if cmd[0] == "sh" and cmd[1] == "-c" and len(cmd) >= 6 and cmd[3] == "relay-snapshot":
            sockets = cmd[6:]
            return ok(state.snapshot_text(sockets))
        # tmux commands
        if cmd[0] == "tmux" and cmd[1] == "-S":
            socket = cmd[2]
            sub = cmd[3]
            if sub == "new-session":
                name = _flag_value(cmd, "-s")
                server_pid, sid, created, pid = state.new_session(socket, name)
                out = DELIM.join([str(server_pid), sid, str(created), pid])
                return ok(out + "\n")
            if sub == "kill-session":
                sid = _flag_value(cmd, "-t")
                state.kill(socket, sid)
                return ok()
            if sub == "has-session":
                sid = _flag_value(cmd, "-t")
                return ok() if sid in state.sessions.get(socket, {}) else fail(1)
            if sub == "capture-pane":
                pane = _flag_value(cmd, "-t")
                return ok(state.captures.get(pane, "preview server log line 1\nline 2\n"))
            return ok()
        return ok()

    return handler


def _in_container_cmd(args: list[str]) -> list[str]:
    if not args or args[0] != "exec":
        return []
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


def _flag_value(cmd: list[str], flag: str) -> str:
    for idx, tok in enumerate(cmd):
        if tok == flag and idx + 1 < len(cmd):
            return cmd[idx + 1]
    return ""


# -- fixtures ----------------------------------------------------------------

@pytest.fixture
def tmux_state() -> FakeTmuxState:
    return FakeTmuxState()


@pytest.fixture
def wired_rt(rt: Runtime, fake_podman: FakePodman, tmux_state: FakeTmuxState) -> Runtime:
    fake_podman.handler = make_handler(tmux_state)
    return rt


def free_port() -> int:
    with closing(socketlib.socket(socketlib.AF_INET, socketlib.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _insert_preview(rt: Runtime, *, env: str = "work2", port: int, name: str = "app", test_command=None) -> str:
    row = await previews.create_preview(
        rt,
        {
            "env": env,
            "name": name,
            "command": "npm run dev",
            "port": port,
            "project": "",
            "health_path": "/",
            "test_command": test_command,
        },
    )
    return row["id"]


# -- port validation ---------------------------------------------------------

async def test_host_networking_accepts_high_port(wired_rt: Runtime):
    info = ContainerInfo(name="job-work2", exists=True, running=True, network_mode="host", ports={})
    hp = previews.resolve_host_port(info, 8100, own_port=8765)
    assert hp == 8100


async def test_host_networking_rejects_own_port(wired_rt: Runtime):
    info = ContainerInfo(name="job-work2", exists=True, running=True, network_mode="host", ports={})
    with pytest.raises(ApiError) as exc:
        previews.resolve_host_port(info, 8765, own_port=8765)
    assert exc.value.code == "invalid_port"


async def test_host_networking_rejects_low_port(wired_rt: Runtime):
    info = ContainerInfo(name="job-work2", exists=True, running=True, network_mode="host", ports={})
    with pytest.raises(ApiError):
        previews.resolve_host_port(info, 80, own_port=8765)


async def test_slirp_requires_published_port(wired_rt: Runtime):
    info = ContainerInfo(name="job-work1", exists=True, running=True, network_mode="slirp4netns", ports={5173: 15173})
    assert previews.resolve_host_port(info, 5173, own_port=8765) == 15173
    with pytest.raises(ApiError) as exc:
        previews.resolve_host_port(info, 3000, own_port=8765)
    assert exc.value.code == "port_not_published"
    assert exc.value.extra["available"] == [5173]


# -- start / port in use -----------------------------------------------------

async def test_start_refuses_when_port_already_listens(wired_rt: Runtime):
    port = free_port()
    with closing(socketlib.socket(socketlib.AF_INET, socketlib.SOCK_STREAM)) as srv:
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        preview_id = await _insert_preview(wired_rt, port=port)
        with pytest.raises(ApiError) as exc:
            await previews.start_preview(wired_rt, preview_id)
    assert exc.value.code == "port_in_use"
    assert exc.value.extra["managed"] is False


# -- readiness ---------------------------------------------------------------

async def test_readiness_reaches_ready(wired_rt: Runtime):
    # A tiny asyncio HTTP server; it starts listening only after start_preview,
    # so the port-in-use guard passes and readiness then finds it.
    port = free_port()

    async def handle(reader, writer):
        await reader.read(1024)
        body = b"ok"
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n" + body)
        await writer.drain()
        writer.close()

    preview_id = await _insert_preview(wired_rt, port=port)
    server_row = await previews.start_preview(wired_rt, preview_id)
    live = previews.latest_server(wired_rt.db, preview_id)
    assert live["status"] == "starting"
    server = await asyncio.start_server(handle, "127.0.0.1", port)
    try:
        # Let the background readiness task run.
        for _ in range(30):
            await asyncio.sleep(0.1)
            live = previews.latest_server(wired_rt.db, preview_id)
            if live["status"] == "ready":
                break
        assert live["status"] == "ready"
        assert live["ready_at"] is not None
    finally:
        server.close()
        await server.wait_closed()
        for t in list(wired_rt.background):
            t.cancel()


async def test_readiness_marks_exited_when_session_gone(wired_rt: Runtime, tmux_state: FakeTmuxState):
    # Use a port with nothing listening so readiness keeps polling; then drop the session.
    port = free_port()
    preview_id = await _insert_preview(wired_rt, port=port)
    row = await previews.start_preview(wired_rt, preview_id)
    server = previews.latest_server(wired_rt.db, preview_id)
    # Remove the tmux session so the next poll sees it gone.
    tmux_state.sessions.clear()
    for _ in range(30):
        await asyncio.sleep(0.1)
        live = previews.latest_server(wired_rt.db, preview_id)
        if live["status"] == "exited":
            break
    assert live["status"] == "exited"
    for t in list(wired_rt.background):
        t.cancel()


# -- stop --------------------------------------------------------------------

async def test_stop_refuses_unmanaged_server(wired_rt: Runtime, tmux_state: FakeTmuxState):
    port = free_port()
    preview_id = await _insert_preview(wired_rt, port=port)
    await previews.start_preview(wired_rt, preview_id)
    # Session vanishes -> no managed live server.
    tmux_state.sessions.clear()
    with pytest.raises(ApiError) as exc:
        await previews.stop_preview(wired_rt, preview_id)
    assert exc.value.code == "not_managed"
    for t in list(wired_rt.background):
        t.cancel()


async def test_stop_kills_managed_server(wired_rt: Runtime, tmux_state: FakeTmuxState):
    port = free_port()
    preview_id = await _insert_preview(wired_rt, port=port)
    await previews.start_preview(wired_rt, preview_id)
    for t in list(wired_rt.background):
        t.cancel()
    await previews.stop_preview(wired_rt, preview_id)
    server = previews.latest_server(wired_rt.db, preview_id)
    assert server["status"] == "stopped"
    assert tmux_state.sessions.get(wired_rt.settings.env("work2").previews_socket, {}) == {}


# -- reconciliation ----------------------------------------------------------

async def test_reconcile_marks_vanished_as_exited(wired_rt: Runtime, tmux_state: FakeTmuxState):
    port = free_port()
    preview_id = await _insert_preview(wired_rt, port=port)
    await previews.start_preview(wired_rt, preview_id)
    for t in list(wired_rt.background):
        t.cancel()
    tmux_state.sessions.clear()
    await previews.reconcile_once(wired_rt)
    live = previews.latest_server(wired_rt.db, preview_id)
    assert live["status"] == "exited"


async def test_reconcile_keeps_live_session(wired_rt: Runtime):
    port = free_port()
    preview_id = await _insert_preview(wired_rt, port=port)
    await previews.start_preview(wired_rt, preview_id)
    for t in list(wired_rt.background):
        t.cancel()
    await previews.reconcile_once(wired_rt)
    live = previews.latest_server(wired_rt.db, preview_id)
    assert live["status"] in ("starting", "ready")


# -- delete ------------------------------------------------------------------

async def test_delete_refuses_running_preview(wired_rt: Runtime):
    port = free_port()
    preview_id = await _insert_preview(wired_rt, port=port)
    await previews.start_preview(wired_rt, preview_id)
    for t in list(wired_rt.background):
        t.cancel()
    with pytest.raises(ApiError) as exc:
        await previews.delete_preview(wired_rt, preview_id)
    assert exc.value.code == "running"


# -- validation --------------------------------------------------------------

async def test_create_rejects_bad_name(wired_rt: Runtime):
    with pytest.raises(ApiError) as exc:
        await previews.create_preview(
            wired_rt,
            {"env": "work2", "name": "bad/name", "command": "x", "port": 8100, "project": ""},
        )
    assert exc.value.code == "invalid_name"


async def test_create_rejects_command_with_newline(wired_rt: Runtime):
    with pytest.raises(ApiError) as exc:
        await previews.create_preview(
            wired_rt,
            {"env": "work2", "name": "app", "command": "a\nb", "port": 8100, "project": ""},
        )
    assert exc.value.code == "invalid_command"


async def test_project_rejects_dotdot(wired_rt: Runtime):
    with pytest.raises(ApiError) as exc:
        await previews.resolve_project(wired_rt, wired_rt.settings.env("work2"), "../etc")
    assert exc.value.code == "invalid_project"


async def test_duplicate_name(wired_rt: Runtime):
    await _insert_preview(wired_rt, port=8100, name="app")
    with pytest.raises(ApiError) as exc:
        await _insert_preview(wired_rt, port=8101, name="app")
    assert exc.value.code == "duplicate_name"


def test_slug():
    assert previews.slug("My App 2") == "my-app-2"
    assert previews.slug("!!!") == "preview"


def test_build_urls_never_generates_an_unverified_tailnet_url(rt: Runtime):
    # A tailnet URL appears only after a gateway route proof (routes_previews.phone_view).
    rt2 = rt
    object.__setattr__(rt2.settings, "tailscale_hostname", "laptop.ts.net")
    urls = previews.build_urls(rt2, 8100)
    assert urls["local"] == "http://127.0.0.1:8100"
    assert urls["tailnet"] is None



# A real work1 /proc/net/tcp excerpt shape: 0x1F40 = 8000 listening (0A), 0x224B = 8779 established (01).
PROC_NET_TCP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:1F40 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 1054267 1
   1: 0100007F:224B 0100007F:A1B2 01 00000000:00000000 00:00000000 00000000  1000        0 1057717 1
"""


def test_proc_net_listening_parses_listen_state_only():
    assert previews.proc_net_listening(PROC_NET_TCP, 8000)
    assert not previews.proc_net_listening(PROC_NET_TCP, 8779)  # established, not listening
    assert not previews.proc_net_listening(PROC_NET_TCP, 8787)


async def test_slirp_listening_uses_the_container_tcp_table(wired_rt: Runtime):
    # With slirp4netns, rootlessport always accepts on the published host port,
    # so a host connect would report "listening" for an idle container port.
    from relay.podman import ContainerInfo

    info = ContainerInfo(name="job-work1", exists=True, running=True, network_mode="slirp4netns", ports={8000: 18000})
    env = wired_rt.settings.env("work1")
    base = wired_rt.podman.handler

    def handler(args, input_bytes):
        cmd = _in_container_cmd(args)
        if cmd[:2] == ["sh", "-c"] and "/proc/net/tcp" in cmd[2]:
            return ok(PROC_NET_TCP)
        return base(args, input_bytes)

    wired_rt.podman.handler = handler
    assert await previews.server_listening(wired_rt, env, info, 8000, 18000)
    assert not await previews.server_listening(wired_rt, env, info, 8787, 18787)
