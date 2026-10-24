"""Tests for relay.sessions and relay.routes_sessions.

These reuse the tmux simulator from test_inventory. Routes are exercised
through a minimal FastAPI app that includes the sessions router, overrides
``require_user``, and maps ``ApiError`` to a JSON response.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))

from test_inventory import TmuxSim, socket_of, wire  # noqa: E402

from relay import inventory, sessions  # noqa: E402
from relay.errors import ApiError  # noqa: E402
from relay.runtime import Principal, Runtime, require_user  # noqa: E402
from relay.routes_sessions import router as sessions_router  # noqa: E402


@pytest.fixture
def sim() -> TmuxSim:
    return TmuxSim()


@pytest.fixture(autouse=True)
def _patch_codex(monkeypatch):
    """Provide relay.provision.codex_notify_override for codex tests."""
    import types

    mod = types.ModuleType("relay.provision")
    mod.codex_notify_override = lambda env, relay_session_id=None: f"notify=relay:{env.id}"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "relay.provision", mod)
    return mod


async def _seed_env_running(rt: Runtime, sim: TmuxSim, env_id: str) -> None:
    """Populate agent availability and container status for one env."""
    wire(rt, sim)
    await inventory.refresh_env(rt, env_id)


def app_for(rt: Runtime) -> FastAPI:
    app = FastAPI()
    app.state.rt = rt
    app.include_router(sessions_router)
    app.dependency_overrides[require_user] = lambda: Principal(via="bearer")

    @app.exception_handler(ApiError)
    async def _handle(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(exc.body(), status_code=exc.status)

    return app


# -- validation --------------------------------------------------------------


def test_validate_name_rules():
    assert sessions.validate_name(" agent1 ") == "agent1"
    for bad in ("", "..", "a/b", "x" * 41, "-lead"):
        with pytest.raises(ApiError) as ei:
            sessions.validate_name(bad)
        assert ei.value.code == "invalid_name"


def test_validate_name_allows_lead_letter_then_symbols():
    assert sessions.validate_name("a-b_c 1") == "a-b_c 1"


async def test_next_agent_name_skips_taken(rt, sim):
    s1 = socket_of(rt, "work1")
    sim.add_session("job-work1", s1, "agent1", command="claude")
    sim.add_session("job-work1", s1, "agent2", command="claude")
    await _seed_env_running(rt, sim, "work1")
    assert sessions.next_agent_name(rt, "work1") == "agent3"


async def test_resolve_project_confined(rt, sim):
    await _seed_env_running(rt, sim, "work1")
    env = rt.settings.env("work1")
    assert await sessions.resolve_project(rt, env, "") == "/home/node/projects"
    assert await sessions.resolve_project(rt, env, "app") == "/home/node/projects/app"
    for bad in ("../etc", "a/../../b", "..", "nope"):
        with pytest.raises(ApiError) as ei:
            await sessions.resolve_project(rt, env, bad)
        assert ei.value.code == "invalid_project"


def test_build_agent_argv_claude_prompt_is_separate_element():
    env = None
    from relay.config import EnvConfig
    from pathlib import Path as _P

    env = EnvConfig(id="work2", label="W2", container="job-work2", host_home=_P("/tmp/x"))
    rid = uuid.uuid4().hex
    argv = sessions.build_agent_argv(
        env, "claude", relay_id=rid, name="agent1",
        permission_mode="acceptEdits", prompt="rm -rf /; echo hi", notify_override=None,
    )
    assert argv[0] == "claude"
    assert "--session-id" in argv and str(uuid.UUID(rid)) in argv
    assert "--permission-mode" in argv and "acceptEdits" in argv
    assert argv[-2] == "--"
    assert argv[-1] == "rm -rf /; echo hi"  # one element, never split


def test_build_agent_argv_codex_uses_notify_override():
    from relay.config import EnvConfig
    from pathlib import Path as _P

    env = EnvConfig(id="work2", label="W2", container="job-work2", host_home=_P("/tmp/x"))
    argv = sessions.build_agent_argv(
        env, "codex", relay_id=uuid.uuid4().hex, name="agent1",
        permission_mode=None, prompt="do it", notify_override="notify=relay:work2",
    )
    assert argv[:4] == ["codex", "-c", "notify=relay:work2", "--"]
    assert argv[-1] == "do it"


# -- create ------------------------------------------------------------------


async def test_create_claude_session(rt, sim):
    await _seed_env_running(rt, sim, "work2")
    view = await sessions.create_session(
        rt, env_id="work2", kind="claude", name="mybot", project="app",
        prompt="hello", permission_mode="acceptEdits",
    )
    assert view["kind"] == "claude"
    assert view["name"] == "mybot"
    assert view["status"] == "live"
    assert view["project_dir"] == "/home/node/projects/app"
    # agent_session_id is the dashed relay id.
    assert view["agent_session_id"] == str(uuid.UUID(view["id"]))
    # The row reconciled through @relay_id, so tmux ids are present.
    assert view["tmux"]["session_id"] and view["tmux"]["pane_id"]


async def test_create_duplicate_name_case_insensitive(rt, sim):
    s2 = socket_of(rt, "work2")
    sim.add_session("job-work2", s2, "Agent1", command="codex")
    await _seed_env_running(rt, sim, "work2")
    with pytest.raises(ApiError) as ei:
        await sessions.create_session(
            rt, env_id="work2", kind="claude", name="agent1", project="", prompt=None, permission_mode=None,
        )
    assert ei.value.code == "duplicate_name"


async def test_create_traversal_rejected(rt, sim):
    await _seed_env_running(rt, sim, "work2")
    with pytest.raises(ApiError) as ei:
        await sessions.create_session(
            rt, env_id="work2", kind="claude", name="x", project="../etc", prompt=None, permission_mode=None,
        )
    assert ei.value.code == "invalid_project"


async def test_create_invalid_permission_mode_rejected(rt, sim):
    await _seed_env_running(rt, sim, "work2")
    with pytest.raises(ApiError) as ei:
        await sessions.create_session(
            rt, env_id="work2", kind="claude", name="x", project="", prompt=None, permission_mode="godmode",
        )
    assert ei.value.code == "invalid_request"


async def test_create_codex_unavailable_in_work1(rt, sim):
    # work1 has claude only.
    sim.server("job-work1", socket_of(rt, "work1")).agents = ("claude",)
    await _seed_env_running(rt, sim, "work1")
    with pytest.raises(ApiError) as ei:
        await sessions.create_session(
            rt, env_id="work1", kind="codex", name="x", project="", prompt=None, permission_mode=None,
        )
    assert ei.value.code == "kind_unavailable"


# -- end ---------------------------------------------------------------------


async def test_end_scope_changed_on_pane_count(rt, sim):
    s2 = socket_of(rt, "work2")
    sim.add_session("job-work2", s2, "agent1", command="codex", panes=2)
    await _seed_env_running(rt, sim, "work2")
    row = rt.db.list_sessions()[0]
    # Client thinks there is 1 pane, but tmux shows 2.
    with pytest.raises(ApiError) as ei:
        await sessions.end_session(
            rt, row["id"], tmux_session_id=row["tmux_session_id"],
            tmux_created=row["tmux_created"], pane_count=1,
        )
    assert ei.value.code == "scope_changed"
    assert ei.value.extra["pane_count"] == 2
    # The session was not killed.
    assert sim.killed == []
    assert rt.db.get_session(row["id"])["status"] == "live"


async def test_end_success(rt, sim):
    s2 = socket_of(rt, "work2")
    sim.add_session("job-work2", s2, "agent1", command="codex", panes=1)
    await _seed_env_running(rt, sim, "work2")
    row = rt.db.list_sessions()[0]
    result = await sessions.end_session(
        rt, row["id"], tmux_session_id=row["tmux_session_id"],
        tmux_created=row["tmux_created"], pane_count=1,
    )
    assert result == {"ended": True, "already": False}
    assert len(sim.killed) == 1
    assert rt.db.get_session(row["id"])["status"] == "ended"


# -- prompt ------------------------------------------------------------------


async def test_prompt_refused_while_pending(rt, sim):
    s2 = socket_of(rt, "work2")
    sim.add_session("job-work2", s2, "agent1", command="codex")
    await _seed_env_running(rt, sim, "work2")
    row = rt.db.list_sessions()[0]
    rt.db.update_session(
        row["id"],
        pending_json='{"id":"a","source":"codex","answerable":true}',
    )
    with pytest.raises(ApiError) as ei:
        await sessions.send_prompt(rt, row["id"], "hi")
    assert ei.value.code == "approval_pending"
    assert sim.pasted == []


async def test_prompt_refused_while_dialog_visible(rt, sim):
    s2 = socket_of(rt, "work2")
    sess = sim.add_session("job-work2", s2, "agent1", command="codex")
    await _seed_env_running(rt, sim, "work2")
    row = rt.db.list_sessions()[0]
    pane = sess.panes[0]
    sim.captures[(s2, pane.pane_id)] = "Would you like to run the following command?\n> 1. Yes, proceed (y)\n"
    with pytest.raises(ApiError) as ei:
        await sessions.send_prompt(rt, row["id"], "hi")
    assert ei.value.code == "approval_pending"
    assert sim.pasted == []


async def test_prompt_refused_when_shell_foreground(rt, sim):
    s2 = socket_of(rt, "work2")
    # Codex row but the pane shows a shell (agent exited).
    sim.add_session("job-work2", s2, "agent1", command="bash")
    await _seed_env_running(rt, sim, "work2")
    row = rt.db.list_sessions()[0]
    # Force the kind to codex to simulate a dashboard codex whose agent exited.
    rt.db.update_session(row["id"], kind="codex", origin="dashboard")
    with pytest.raises(ApiError) as ei:
        await sessions.send_prompt(rt, row["id"], "hi")
    assert ei.value.code == "agent_not_running"
    assert sim.pasted == []


async def test_prompt_sent_when_agent_foreground(rt, sim):
    s2 = socket_of(rt, "work2")
    sim.add_session("job-work2", s2, "agent1", command="codex")
    await _seed_env_running(rt, sim, "work2")
    row = rt.db.list_sessions()[0]
    result = await sessions.send_prompt(rt, row["id"], "please run tests")
    assert result == {"sent": True}
    assert len(sim.pasted) == 1
    assert sim.pasted[0][2] == b"please run tests"


async def test_prompt_unsupported_for_shell(rt, sim):
    s2 = socket_of(rt, "work2")
    sim.add_session("job-work2", s2, "shellone", command="bash")
    await _seed_env_running(rt, sim, "work2")
    row = rt.db.list_sessions()[0]
    with pytest.raises(ApiError) as ei:
        await sessions.send_prompt(rt, row["id"], "hi")
    assert ei.value.code == "unsupported"


# -- interrupt ---------------------------------------------------------------


async def test_interrupt_sends_escape(rt, sim):
    s2 = socket_of(rt, "work2")
    sim.add_session("job-work2", s2, "agent1", command="codex")
    await _seed_env_running(rt, sim, "work2")
    row = rt.db.list_sessions()[0]
    await sessions.interrupt(rt, row["id"])
    assert sim.sent_keys and sim.sent_keys[-1][2] == ("Escape",)


async def test_codex_prompt_shows_working_until_turn_complete(rt, sim):
    # Codex reports only turn completion, so relay marks the turn as running
    # when it sends a prompt, and an interrupt returns it to idle.
    s2 = socket_of(rt, "work2")
    sim.add_session("job-work2", s2, "agent1", command="codex")
    await _seed_env_running(rt, sim, "work2")
    row = rt.db.list_sessions()[0]
    rt.db.update_session(row["id"], state="idle", state_source="notify")
    await sessions.send_prompt(rt, row["id"], "next step")
    assert rt.db.get_session(row["id"])["state"] == "working"
    await sessions.interrupt(rt, row["id"])
    assert rt.db.get_session(row["id"])["state"] == "idle"


# -- capabilities and notes --------------------------------------------------


async def test_capabilities_discovered_codex(rt, sim):
    s2 = socket_of(rt, "work2")
    sim.add_session("job-work2", s2, "agent1", command="codex")
    await _seed_env_running(rt, sim, "work2")
    row = rt.db.list_sessions()[0]
    view = sessions.session_view(rt, row)
    caps = view["capabilities"]
    assert caps["terminal"] and caps["prompt"] and caps["interrupt"]
    assert caps["approve"] is False  # codex cannot approve
    assert caps["conversation"] is False  # codex has no conversation view
    assert any("terminal only" in n for n in view["notes"])


async def test_capabilities_dashboard_claude(rt, sim):
    await _seed_env_running(rt, sim, "work2")
    view = await sessions.create_session(
        rt, env_id="work2", kind="claude", name="claudebot", project="", prompt=None, permission_mode=None,
    )
    row = rt.db.get_session(view["id"])
    # Give it a pending answerable approval and a transcript path.
    rt.db.update_session(
        row["id"],
        pending_json='{"id":"a","source":"claude","answerable":true}',
        transcript_path="/home/node/.claude/x.jsonl",
        foreground="claude",
    )
    view2 = sessions.session_view(rt, rt.db.get_session(row["id"]))
    caps = view2["capabilities"]
    assert caps["approve"] is True
    assert caps["conversation"] is True


async def test_capabilities_shell(rt, sim):
    s2 = socket_of(rt, "work2")
    sim.add_session("job-work2", s2, "shellone", command="bash")
    await _seed_env_running(rt, sim, "work2")
    row = rt.db.list_sessions()[0]
    view = sessions.session_view(rt, row)
    caps = view["capabilities"]
    assert caps["terminal"] is True
    assert caps["prompt"] is False and caps["interrupt"] is False
    assert caps["conversation"] is False and caps["resume"] is False
    assert any("Shell sessions" in n for n in view["notes"])


# -- routes ------------------------------------------------------------------


async def test_routes_state_and_get(rt, sim):
    s1 = socket_of(rt, "work1")
    s2 = socket_of(rt, "work2")
    sim.add_session("job-work1", s1, "agent1", command="claude")
    sim.add_session("job-work2", s2, "agent1", command="codex")
    await _seed_env_running(rt, sim, "work1")
    await _seed_env_running(rt, sim, "work2")

    client = TestClient(app_for(rt))
    state = client.get("/api/state").json()
    assert len(state["envs"]) == 2
    assert len(state["sessions"]) == 2
    # Identical names in both envs route to distinct ids.
    ids = {s["id"] for s in state["sessions"]}
    assert len(ids) == 2
    for sess_id in ids:
        got = client.get(f"/api/sessions/{sess_id}")
        assert got.status_code == 200
        assert got.json()["id"] == sess_id


async def test_routes_create_and_end(rt, sim):
    await _seed_env_running(rt, sim, "work2")
    client = TestClient(app_for(rt))
    resp = client.post("/api/sessions", json={"env": "work2", "kind": "claude", "name": "routebot", "project": "app"})
    assert resp.status_code == 201
    view = resp.json()
    sid = view["id"]

    # End with the wrong tmux identity -> scope_changed.
    bad = client.post(f"/api/sessions/{sid}/end", json={"tmux_session_id": "$999", "tmux_created": 1, "pane_count": 1})
    assert bad.status_code == 409
    assert bad.json()["error"] == "scope_changed"

    good = client.post(
        f"/api/sessions/{sid}/end",
        json={"tmux_session_id": view["tmux"]["session_id"], "tmux_created": view["tmux"]["created"], "pane_count": 1},
    )
    assert good.status_code == 200
    assert good.json()["ended"] is True


async def test_routes_projects_and_unknown_env(rt, sim):
    await _seed_env_running(rt, sim, "work2")
    client = TestClient(app_for(rt))
    resp = client.get("/api/envs/work2/projects")
    assert resp.status_code == 200
    body = resp.json()
    assert body["root"] == "/home/node/projects"
    assert {p["name"] for p in body["projects"]} == {"app", "site"}

    missing = client.get("/api/envs/nope/projects")
    assert missing.status_code == 404
    assert missing.json()["error"] == "unknown_env"


async def test_routes_prompt_validation(rt, sim):
    s2 = socket_of(rt, "work2")
    sim.add_session("job-work2", s2, "agent1", command="codex")
    await _seed_env_running(rt, sim, "work2")
    row = rt.db.list_sessions()[0]
    client = TestClient(app_for(rt))
    empty = client.post(f"/api/sessions/{row['id']}/prompt", json={"text": "   "})
    assert empty.status_code == 400
    assert empty.json()["error"] == "invalid_prompt"


# -- resume ------------------------------------------------------------------


async def test_resume_creates_new_row(rt, sim):
    s2 = socket_of(rt, "work2")
    sim.add_session("job-work2", s2, "agent1", command="codex")
    await _seed_env_running(rt, sim, "work2")
    row = rt.db.list_sessions()[0]
    # Make it an ended dashboard agent with a saved thread id.
    rt.db.update_session(
        row["id"], status="ended", origin="dashboard",
        agent_session_id="thread-123", ended_at=1.0, end_reason="closed",
    )
    # The tmux session is gone (it ended).
    sim.servers[("job-work2", s2)].sessions.clear()
    await inventory.refresh_env(rt, "work2")
    view = await sessions.resume_session(rt, row["id"])
    assert view["status"] == "live"
    assert view["id"] != row["id"]
    assert view["agent_session_id"] == "thread-123"



async def test_resume_right_after_end_reuses_the_name(rt, sim):
    # End must refresh the cached snapshot, or an immediate Resume sees the
    # killed tmux session's name as taken and returns duplicate_name.
    s2 = socket_of(rt, "work2")
    sim.add_session("job-work2", s2, "agent1", command="codex", panes=1)
    await _seed_env_running(rt, sim, "work2")
    row = rt.db.list_sessions()[0]
    rt.db.update_session(row["id"], origin="dashboard", agent_session_id="thread-9")
    row = rt.db.get_session(row["id"])
    await sessions.end_session(
        rt, row["id"], tmux_session_id=row["tmux_session_id"],
        tmux_created=row["tmux_created"], pane_count=1,
    )
    view = await sessions.resume_session(rt, row["id"])
    assert view["status"] == "live"
    assert view["name"] == "agent1"
    assert view["agent_session_id"] == "thread-9"


# Real Claude 2.1 folder trust screen (captured live on 2026-09-30, path changed).
TRUST_SCREEN = """ Accessing workspace:
 /home/node/projects/app
 Quick safety check: Is this a project
 you created or one you trust? (Like
 your own code, a well-known open
 source project, or work from your
 team).
 Security guide
 \u276f No, exit
   Yes, I trust this folder
 Enter to confirm \u00b7 Esc to cancel
"""


# Real Codex 0.159 folder trust screen (captured live on 2026-09-30).
CODEX_TRUST_SCREEN = """  Trust this folder? Codex can read,
  edit, and run files here, subject
  to your permission settings.
\u203a 1. Trust and continue
  2. Quit
  enter continue \u00b7 esc quit
"""


def test_trust_dialog_patterns_match_real_screens():
    from relay import screen

    assert screen.trust_dialog_visible(TRUST_SCREEN)
    assert screen.trust_dialog_visible(CODEX_TRUST_SCREEN)
    assert not screen.trust_dialog_visible("Do you want to proceed?\n\u276f 1. Yes\n  2. No")

async def test_startup_watch_surfaces_and_clears_trust_dialog(rt, sim):
    import asyncio

    s2 = socket_of(rt, "work2")
    sim.add_session("job-work2", s2, "agent1", command="claude")
    await _seed_env_running(rt, sim, "work2")
    row = rt.db.list_sessions()[0]
    rt.db.update_session(row["id"], origin="dashboard")
    sim.captures[(s2, row["pane_id"])] = TRUST_SCREEN

    task = asyncio.create_task(
        sessions.watch_startup(rt, row["id"], duration=2, interval=0.02, answer_timeout=2)
    )
    for _ in range(100):
        await asyncio.sleep(0.02)
        if rt.db.get_session(row["id"])["pending_json"]:
            break
    view = sessions.session_view(rt, rt.db.get_session(row["id"]))
    assert view["state"] == "needs_input"
    assert view["pending"]["tool"] == "Folder trust"
    assert view["pending"]["answerable"] is False
    assert not view["capabilities"]["approve"]
    # A prompt must never be typed into the trust dialog.
    with pytest.raises(ApiError) as exc:
        await sessions.send_prompt(rt, row["id"], "hello")
    assert exc.value.code == "approval_pending"
    assert sim.pasted == []

    # The user answers in the terminal; the dialog disappears.
    sim.captures[(s2, row["pane_id"])] = "Claude Code\n> "
    await asyncio.wait_for(task, 3)
    after = rt.db.get_session(row["id"])
    assert after["pending_json"] is None
    assert after["state_source"] == "terminal"

# -- live --------------------------------------------------------------------


@pytest.mark.live
async def test_live_refresh_env_distinct_agent1(tmp_path):
    """Read-only: snapshot both real containers and confirm agent1 in each.

    This test only inspects and snapshots. It never creates, kills, or writes
    to any tmux session, and it uses a temporary database.
    """
    from relay.config import EnvConfig, Settings
    from relay.db import Database
    from relay.podman import Podman
    from relay.runtime import build_runtime

    envs = {
        "work1": EnvConfig(id="work1", label="Work 1", container="job-work1", host_home=tmp_path / "w1"),
        "work2": EnvConfig(id="work2", label="Work 2", container="job-work2", host_home=tmp_path / "w2"),
    }
    for e in envs.values():
        e.host_home.mkdir(parents=True)
    settings = Settings(state_dir=tmp_path / "state", envs=envs, frontend_dist=tmp_path / "dist", browser_executable=None)
    (tmp_path / "state").mkdir()
    live_rt = build_runtime(settings, podman=Podman(), db=Database(settings.db_path))
    try:
        await inventory.refresh_env(live_rt, "work1")
        await inventory.refresh_env(live_rt, "work2")
        rows = live_rt.db.list_sessions()
        by_env: dict[str, list] = {"work1": [], "work2": []}
        for r in rows:
            if r["display_name"] == "agent1":
                by_env[r["env"]].append(r["id"])
        assert by_env["work1"], "expected an agent1 in work1"
        assert by_env["work2"], "expected an agent1 in work2"
        # Distinct dashboard ids per environment.
        assert set(by_env["work1"]).isdisjoint(set(by_env["work2"]))
    finally:
        live_rt.db.close()
