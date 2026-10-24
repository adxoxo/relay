"""Tests for the terminal bridge.

Unit tests cover the OSC 7770 marker parser (including a split across reads)
and the resize validation. One ``@pytest.mark.live`` test drives a real tmux
client inside ``job-work2`` on the dedicated ``relay-test`` socket.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from starlette.websockets import WebSocketDisconnect
from fastapi.testclient import TestClient

from relay import terminal
from relay.app import create_app
from relay.auth import Auth
from relay.terminal import _valid_size, parse_marker

BASE = "http://127.0.0.1:8765"
LIVE_SOCKET = "/tmp/tmux-1000/relay-test"


# -- ping / pong ---------------------------------------------------------------

class _JsonSink:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, obj: dict) -> None:
        self.sent.append(obj)


@pytest.mark.parametrize(
    ("ping", "pong"),
    [
        ({"type": "ping"}, {"type": "pong"}),  # legacy heartbeat
        ({"type": "ping", "nonce": "w3k9"}, {"type": "pong", "nonce": "w3k9"}),  # wake probe
        ({"type": "ping", "nonce": 7}, {"type": "pong"}),
        ({"type": "ping", "nonce": ""}, {"type": "pong"}),
        ({"type": "ping", "nonce": "x" * 65}, {"type": "pong"}),
    ],
)
def test_ping_answers_with_its_nonce_only(rt, ping, pong):
    bridge = terminal.TerminalBridge(rt, rt.settings.envs["work1"], "/tmp/x", "$1", "s1")
    sink = _JsonSink()
    bridge._ws = sink  # type: ignore[assignment]
    asyncio.run(bridge._handle_json(ping))
    assert sink.sent == [pong]


# -- marker parser -----------------------------------------------------------

def test_parse_marker_basic():
    buf = b"\x1b]7770;relay-client;12345\x07hello world"
    pid, cleaned, remainder = parse_marker(buf)
    assert pid == 12345
    assert cleaned == b"hello world"
    assert remainder == b""


def test_parse_marker_with_leading_and_trailing_bytes():
    buf = b"pre\x1b]7770;relay-client;77\x07post"
    pid, cleaned, remainder = parse_marker(buf)
    assert pid == 77
    assert cleaned == b"prepost"


def test_parse_marker_st_terminator():
    buf = b"\x1b]7770;relay-client;9\x1b\\rest"
    pid, cleaned, remainder = parse_marker(buf)
    assert pid == 9
    assert cleaned == b"rest"


def test_parse_marker_split_across_reads():
    # The marker arrives in two chunks. The parser holds back the partial ESC.
    first = b"output\x1b]7770;relay-cli"
    pid, cleaned, remainder = parse_marker(first)
    assert pid is None
    assert cleaned == b"output"
    assert remainder == b"\x1b]7770;relay-cli"
    # Feed the accumulated buffer once the rest arrives.
    second = remainder + b"ent;4242\x07tail"
    pid, cleaned, remainder = parse_marker(second)
    assert pid == 4242
    assert cleaned == b"tail"
    assert remainder == b""


def test_parse_marker_no_marker_plain_bytes():
    pid, cleaned, remainder = parse_marker(b"just terminal output\n")
    assert pid is None
    assert cleaned == b"just terminal output\n"
    assert remainder == b""


def test_process_output_strips_marker_across_reads(rt):
    env = rt.settings.envs["work2"]
    bridge = terminal.TerminalBridge(rt, env, "/tmp/tmux-1000/relay-test", "$1", "sess")
    # First read holds a partial marker: nothing forwarded but the safe prefix.
    out1 = bridge._process_output(b"A\x1b]7770;relay-cli")
    assert out1 == b"A"
    assert bridge.container_pid is None
    # Second read completes the marker.
    out2 = bridge._process_output(b"ent;555\x07B")
    assert out2 == b"B"
    assert bridge.container_pid == 555
    # After the marker, everything passes through untouched.
    assert bridge._process_output(b"\x1b]7770;fake\x07C") == b"\x1b]7770;fake\x07C"


# -- resize validation -------------------------------------------------------

@pytest.mark.parametrize(
    "cols,rows,expected",
    [
        (120, 40, (120, 40)),
        (20, 5, (20, 5)),
        (500, 200, (500, 200)),
        (19, 40, None),
        (501, 40, None),
        (120, 4, None),
        (120, 201, None),
        ("x", 40, None),
        (None, None, None),
    ],
)
def test_valid_size(cols, rows, expected):
    assert _valid_size(cols, rows) == expected


# -- close codes for a bad session -------------------------------------------

@pytest.fixture
def app(rt):
    return create_app(runtime=rt, features=False)


@pytest.fixture
def token(rt) -> str:
    return Auth(rt.settings, rt.db).ensure_token()


def _auth_cookie(client: TestClient, token: str) -> str:
    client.post("/api/auth/login", json={"token": token})
    return client.cookies.get("relay_session")


def test_terminal_rejects_unauthenticated(app):
    with TestClient(app, base_url=BASE) as client:
        with pytest.raises(Exception) as exc_info:
            with client.websocket_connect("/api/sessions/none/terminal"):
                pass
        assert getattr(exc_info.value, "code", None) == 4401


def test_terminal_unknown_session(app, rt, token):
    with TestClient(app, base_url=BASE) as client:
        cookie = _auth_cookie(client, token)
        with client.websocket_connect(
            "/api/sessions/does-not-exist/terminal",
            headers={"Cookie": f"relay_session={cookie}"},
        ) as ws:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_bytes()
        assert exc_info.value.code == 4404


def test_terminal_not_live(app, rt, token):
    sid = rt.db.insert_session(
        dict(
            env="work2",
            kind="shell",
            display_name="s",
            tmux_name="s",
            origin="dashboard",
            status="ended",
            socket_path=LIVE_SOCKET,
        )
    )
    with TestClient(app, base_url=BASE) as client:
        cookie = _auth_cookie(client, token)
        # Authenticated callers are accepted first, so the browser receives
        # the close code and stops reconnecting to a dead session.
        with client.websocket_connect(
            f"/api/sessions/{sid}/terminal",
            headers={"Cookie": f"relay_session={cookie}"},
        ) as ws:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_bytes()
        assert exc_info.value.code == 4409


# -- live test ---------------------------------------------------------------

async def _list_clients(tmux) -> str:
    res = await tmux.podman.exec(
        tmux.env, ["tmux", "-S", LIVE_SOCKET, "list-clients"], timeout=10
    )
    return res.out


@pytest.mark.live
def test_terminal_live_roundtrip(tmp_path):
    """Attach to a real tmux session in job-work2, type, read output, resize, close."""
    from relay.config import EnvConfig, Settings
    from relay.db import Database
    from relay.podman import Podman
    from relay.runtime import build_runtime
    from relay.tmux import Tmux

    home = tmp_path / "w2"
    home.mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    env = EnvConfig(id="work2", label="Work 2", container="job-work2", host_home=home)
    settings = Settings(
        state_dir=state,
        envs={"work2": env},
        frontend_dist=tmp_path / "dist",
        browser_executable=None,
    )
    rt = build_runtime(settings, podman=Podman(), db=Database(settings.db_path))
    tmux: Tmux = rt.tmux["work2"]

    session_id = pane_id = None

    async def _setup():
        nonlocal session_id, pane_id
        sid, pid, created, server_pid = await tmux.new_session(
            LIVE_SOCKET,
            name="relaytest",
            cwd="/home/node",
            argv=["bash", "--norc", "-i"],
        )
        session_id, pane_id = sid, pid
        return sid, created, server_pid

    sid, created, server_pid = asyncio.run(_setup())

    row_id = rt.db.insert_session(
        dict(
            env="work2",
            kind="shell",
            display_name="relaytest",
            tmux_name="relaytest",
            origin="dashboard",
            status="live",
            state="idle",
            socket_path=LIVE_SOCKET,
            server_pid=server_pid,
            tmux_session_id=sid,
            tmux_created=created,
            pane_id=pane_id,
        )
    )

    app = create_app(runtime=rt, features=False)
    try:
        with TestClient(app, base_url=BASE) as client:
            token = rt.auth.ensure_token()
            cookie = _auth_cookie(client, token)
            with client.websocket_connect(
                f"/api/sessions/{row_id}/terminal?cols=100&rows=30",
                headers={"Cookie": f"relay_session={cookie}"},
            ) as ws:
                # First a JSON 'ready', then terminal bytes.
                ready = ws.receive_json()
                assert ready["type"] == "ready"
                ws.send_json({"type": "input", "data": "echo relay-ok-$((6*7))\n"})
                collected = b""
                for _ in range(200):
                    msg = ws.receive()
                    data = msg.get("bytes")
                    if data:
                        collected += data
                        if b"relay-ok-42" in collected:
                            break
                assert b"relay-ok-42" in collected
                ws.send_json({"type": "resize", "cols": 120, "rows": 40})
                # Let the resize apply.
                import time

                time.sleep(0.3)
                # The bridge must have captured the container client PID from
                # the OSC marker so close() can SIGHUP it.
                row = rt.db.one("SELECT container_pid FROM terminal_clients LIMIT 1")
                assert row is not None and row["container_pid"], "marker PID not captured"
    finally:
        # The bridge close ran on disconnect and sent SIGHUP to the container
        # client. tmux drops the client asynchronously, so poll briefly.
        async def _teardown() -> str:
            clients = ""
            for _ in range(20):
                clients = await _list_clients(tmux)
                if clients.strip() == "":
                    break
                await asyncio.sleep(0.1)
            await tmux.kill_session(LIVE_SOCKET, sid)
            # Kill the now-empty server and remove the socket inode.
            with contextlib.suppress(Exception):
                await tmux.podman.exec(
                    tmux.env, ["sh", "-c", "tmux -S \"$1\" kill-server 2>/dev/null; rm -f \"$1\"", "relay-clean", LIVE_SOCKET]
                )
            return clients

        clients = asyncio.run(_teardown())
        rt.db.close()

    assert clients.strip() == ""
