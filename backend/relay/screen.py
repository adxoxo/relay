"""Heuristics over captured terminal screens.

Terminal text is not a reliable source for every state. relay uses these
checks only as a guard before it types into an agent: a prompt or an approval
answer is sent only when the screen agrees with the structured event state.

The adapters module owns and refines these patterns after live tests.
"""

from __future__ import annotations

import re

# Claude Code permission dialog, for example:
#   Do you want to proceed?
#   ❯ 1. Yes
#     2. Yes, and don't ask again for ...
#     3. No, and tell Claude what to do differently (esc)
_CLAUDE_QUESTION = re.compile(r"Do you want to (proceed|make this edit|create|allow|run|fetch|use)\b", re.I)
_NUMBERED_YES = re.compile(r"^\s*(?:[❯>›▶]\s*)?1\.\s+Yes\b", re.M)
_HIGHLIGHTED_YES = re.compile(r"^\s*[❯>›▶]\s*1\.\s+Yes\b", re.M)

# Codex approval overlay, for example:
#   Would you like to run the following command?
#   › 1. Yes, proceed (y)
_CODEX_QUESTION = re.compile(
    r"(Would you like to (run the following command|make the following edits)|Allow Codex to|Approve this)",
    re.I,
)


def _tail(text: str, lines: int) -> str:
    return "\n".join(text.rstrip("\n").splitlines()[-lines:])


def permission_dialog_visible(screen: str, *, tail_lines: int = 40) -> bool:
    """True when the bottom of the screen shows a Claude or Codex approval question."""
    tail = _tail(screen, tail_lines)
    if _CLAUDE_QUESTION.search(tail) and _NUMBERED_YES.search(tail):
        return True
    return bool(_CODEX_QUESTION.search(tail))


def claude_yes_highlighted(screen: str, *, tail_lines: int = 40) -> bool:
    """True when Claude's dialog shows option 1 ("Yes") as the selected option."""
    tail = _tail(screen, tail_lines)
    return bool(_CLAUDE_QUESTION.search(tail) and _HIGHLIGHTED_YES.search(tail))


# Folder trust dialog shown before an agent starts in a new folder. Both were
# captured live on 2026-09-30:
# - Claude 2.1: "Quick safety check ... ❯ No, exit / Yes, I trust this folder"
# - Codex 0.159: "Trust this folder? ... › 1. Trust and continue / 2. Quit"
_TRUST_DIALOG = re.compile(
    r"(Yes, I trust this folder|Do you trust the files in this folder|"
    r"Trust this folder\?|\bTrust and continue\b)",
    re.I,
)


def trust_dialog_visible(screen: str, *, tail_lines: int = 40) -> bool:
    """True when the bottom of the screen shows a folder trust question."""
    return bool(_TRUST_DIALOG.search(_tail(screen, tail_lines)))


# Idle markers for a running agent, captured live on 2026-09-30. Each is a
# positive signal that the agent finished its turn and now waits for input:
# - Claude prints "· done" on its status line only when the turn is complete,
#   for example "✻ Crunched for 37s · done 3:46 PM". A running turn shows the
#   elapsed time without "· done".
# - Codex shows the empty input hint "Ask Codex to do anything" only when no
#   turn is running. A running turn replaces it with a status line.
#   A newer Codex CLI can still leave the empty-composer hint on screen while
#   a turn runs, next to its "esc to interrupt" status line (captured live on
#   work2, 2026-10-11, cli_version 0.162.1). That active-turn marker takes
#   precedence over the idle hint, so a running turn is never read as idle.
_CLAUDE_IDLE = re.compile(r"·\s*done\b", re.I)
_CODEX_IDLE = re.compile(r"Ask Codex to do anything", re.I)
_CODEX_ACTIVE_TURN = re.compile(r"esc to interrupt", re.I)


def agent_idle(screen: str, kind: str, *, tail_lines: int = 40) -> bool:
    """True when a running agent's screen shows a positive idle marker.

    ``kind`` is 'claude' or 'codex'. This is a positive test: an unknown or a
    working screen returns False, so the caller treats it as working. It never
    runs when a permission or trust dialog is on screen (the caller checks
    those first), so a dialog is not mistaken for idle.
    """
    tail = _tail(screen, tail_lines)
    if kind == "claude":
        return bool(_CLAUDE_IDLE.search(tail))
    if kind == "codex":
        return bool(_CODEX_IDLE.search(tail)) and not _CODEX_ACTIVE_TURN.search(tail)
    return False


def agent_screen_state(screen: str, kind: str) -> str:
    """Classify a running agent's screen into a terminal-owned state.

    Order: a permission dialog wins (needs_input), then a positive idle marker
    (idle), otherwise the agent is treated as working. This is only a fallback
    used when no hook or notify event owns the state.
    """
    if permission_dialog_visible(screen):
        return "needs_input"
    if agent_idle(screen, kind):
        return "idle"
    return "working"


def screen_mentions(screen: str, needles: list[str], *, tail_lines: int = 60) -> bool:
    """True when every non-empty needle appears near the bottom of the screen.

    Whitespace is normalised, because the terminal wraps long commands.
    """
    tail = re.sub(r"\s+", " ", _tail(screen, tail_lines))
    for needle in needles:
        n = re.sub(r"\s+", " ", needle).strip()
        if n and n not in tail:
            return False
    return True


# relay's own line from the agent launch wrapper (sessions._AGENT_LAUNCH_SCRIPT):
#   [relay] claude exited with code 1. The terminal stays open.
_LAUNCH_FAILED = re.compile(r"^\[relay\] (\S+) exited with code (\d+)\.", re.M)
_ERROR_LINE = re.compile(r"\berror\b", re.I)
_STACK_FRAME = re.compile(r"^\s*at\s")


def launch_failure(screen: str) -> tuple[str, int, str] | None:
    """Find relay's launch-failure line on a captured screen.

    Returns ``(program, exit_code, reason)`` or ``None``. ``reason`` is the last
    error line printed before the relay line (stack frames skipped), else the
    last few output lines. It is capped at 500 characters.
    """
    matches = list(_LAUNCH_FAILED.finditer(screen))
    if not matches:
        return None
    match = matches[-1]
    before = [line.strip() for line in screen[: match.start()].splitlines()[-40:]]
    lines = [line for line in before if line]
    errors = [line for line in lines if _ERROR_LINE.search(line) and not _STACK_FRAME.match(line)]
    reason = errors[-1] if errors else "\n".join(lines[-4:])
    return match.group(1), int(match.group(2)), reason[:500]
