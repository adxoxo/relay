"""Tests for terminal-screen state classification.

These cover the fallback that gives a by-hand (terminal-owned) agent a live
state, so the dashboard reflects both workspaces even when relay did not start
the agent. The captured strings are real frames from work1 and work2 on
2026-09-30.
"""

from __future__ import annotations

from relay import inventory, screen
from relay.tmux import TmuxPane


# -- captured frames ---------------------------------------------------------

CLAUDE_IDLE = """\
  Ready. What are we building today?

✻ Crunched for 37s · done 3:46 PM

─────────────────────────────────────────
❯ /cockpit-staging check
─────────────────────────────────────────
  Opus 5.5 (1M context) | projects | m…
  ⏵⏵ bypass permissions on (shift+tab
"""

CLAUDE_IDLE_WRAPPED = """\
  "ship it" to open the PR, or
  "drop it" to remove the branch.

✻ Churned for 6m 13s · done
  10:03 PM

───────────────────────────────
❯ ship it
"""

# A running Claude turn shows the spinner line without "· done".
CLAUDE_WORKING = """\
  Working on the plan now.

✻ Churning for 12s (esc to interrupt)

───────────────────────────────
"""

CLAUDE_PERMISSION = """\
Do you want to proceed?
❯ 1. Yes
  2. Yes, and don't ask again
  3. No, and tell Claude what to do differently (esc)
"""

CODEX_IDLE = """\
  • production outcome verified: both resubmitted.

  Worked for 22m 10s • 17:06

› Ask Codex to do anything

  GPT-6.1-Sol low · ~/projects · Test Carfax service in prod · Main [default]
"""

# A running Codex turn replaces the "Ask Codex to do anything" hint.
CODEX_WORKING = """\
• Explored
  └ Read index.md, local-paths.md

  Working (12s · esc to interrupt)
"""

# cli_version 0.162.1 (captured live on work2, 2026-10-11): the empty-composer
# idle hint stays on screen next to the active-turn marker. The active-turn
# marker must win, or a working agent reads as idle.
CODEX_WORKING_WITH_IDLE_HINT = """\
• Explored
  └ Read index.md, local-paths.md

  Working (12s · esc to interrupt)

› Ask Codex to do anything
"""


# -- screen.agent_idle -------------------------------------------------------

def test_claude_idle_frames_are_idle():
    assert screen.agent_idle(CLAUDE_IDLE, "claude") is True
    assert screen.agent_idle(CLAUDE_IDLE_WRAPPED, "claude") is True


def test_claude_working_frame_is_not_idle():
    assert screen.agent_idle(CLAUDE_WORKING, "claude") is False


def test_codex_idle_frame_is_idle():
    assert screen.agent_idle(CODEX_IDLE, "codex") is True


def test_codex_working_frame_is_not_idle():
    assert screen.agent_idle(CODEX_WORKING, "codex") is False


def test_codex_working_frame_with_idle_hint_is_not_idle():
    # The active-turn marker ("esc to interrupt") wins over the idle hint.
    assert screen.agent_idle(CODEX_WORKING_WITH_IDLE_HINT, "codex") is False


def test_agent_idle_unknown_kind_is_false():
    assert screen.agent_idle(CLAUDE_IDLE, "shell") is False


# -- screen.agent_screen_state -----------------------------------------------

def test_agent_screen_state_idle():
    assert screen.agent_screen_state(CLAUDE_IDLE, "claude") == "idle"
    assert screen.agent_screen_state(CODEX_IDLE, "codex") == "idle"


def test_agent_screen_state_working():
    assert screen.agent_screen_state(CLAUDE_WORKING, "claude") == "working"
    assert screen.agent_screen_state(CODEX_WORKING, "codex") == "working"
    assert screen.agent_screen_state(CODEX_WORKING_WITH_IDLE_HINT, "codex") == "working"


def test_agent_screen_state_permission_wins_over_idle():
    # A permission dialog must classify as needs_input, never idle/working.
    assert screen.agent_screen_state(CLAUDE_PERMISSION, "claude") == "needs_input"


# -- inventory._fallback_state -----------------------------------------------

def _pane(command: str) -> TmuxPane:
    return TmuxPane(
        session_id="$1",
        window_id="@1",
        window_index=0,
        window_active=True,
        pane_id="%0",
        pane_index=0,
        pane_active=True,
        pid=1234,
        dead=False,
        width=80,
        height=24,
        activity=0,
        current_command=command,
        current_path="/home/node/projects",
    )


def test_fallback_agent_without_screen_is_unknown():
    # Old behavior preserved: a running agent with no capture stays unknown.
    assert inventory._fallback_state("claude", _pane("claude"), None) == "unknown"


def test_fallback_agent_with_screen_reads_state():
    assert inventory._fallback_state("claude", _pane("claude"), CLAUDE_IDLE) == "idle"
    assert inventory._fallback_state("claude", _pane("claude"), CLAUDE_WORKING) == "working"
    assert inventory._fallback_state("codex", _pane("node"), CODEX_IDLE) == "idle"
    assert inventory._fallback_state("codex", _pane("node"), CODEX_WORKING) == "working"


def test_fallback_agent_shell_foreground_is_exited():
    # An agent whose pane runs a shell has exited, screen text notwithstanding.
    assert inventory._fallback_state("claude", _pane("bash"), CLAUDE_IDLE) == "exited"


def test_fallback_shell_state_unchanged():
    assert inventory._fallback_state("shell", _pane("bash"), None) == "idle"
    assert inventory._fallback_state("shell", _pane("vim"), None) == "working"


# -- inventory._apply_fallback_state (ownership guard) ------------------------

def _insert_agent(rt, *, state, state_source):
    sid = rt.db.insert_session(
        dict(
            env="work1",
            kind="claude",
            display_name="agent1",
            tmux_name="agent1",
            origin="discovered",
            status="live",
            state=state,
            state_source=state_source,
            socket_path="/tmp/tmux-1000/default",
        )
    )
    return rt.db.get_session(sid)


def test_terminal_owned_agent_gets_state_from_screen(rt):
    # A by-hand agent (state_source terminal) is upgraded from unknown to a
    # real state read from its screen.
    row = _insert_agent(rt, state="unknown", state_source="terminal")
    fields: dict = {}
    inventory._apply_fallback_state(row, "claude", _pane("claude"), fields, CLAUDE_WORKING)
    assert fields["state"] == "working"
    assert fields["state_source"] == "terminal"


def test_hook_owned_state_is_not_overwritten_by_screen(rt):
    # A hook source is authoritative. An idle screen must not flip a hook
    # "working" state, so the live agent state stays correct.
    row = _insert_agent(rt, state="working", state_source="hook")
    fields: dict = {}
    inventory._apply_fallback_state(row, "claude", _pane("claude"), fields, CLAUDE_IDLE)
    assert "state" not in fields  # hook owns it, no change


def test_notify_owned_state_is_not_overwritten_by_screen(rt):
    row = _insert_agent(rt, state="idle", state_source="notify")
    fields: dict = {}
    inventory._apply_fallback_state(row, "codex", _pane("node"), fields, CODEX_WORKING)
    assert "state" not in fields  # notify owns it, no change


def test_shell_foreground_overrides_even_hook_state(rt):
    # An agent whose pane runs a shell has exited; this override wins over a
    # stale hook state.
    row = _insert_agent(rt, state="working", state_source="hook")
    fields: dict = {}
    inventory._apply_fallback_state(row, "claude", _pane("bash"), fields, CLAUDE_IDLE)
    assert fields["state"] == "exited"
    assert fields["state_source"] == "terminal"

