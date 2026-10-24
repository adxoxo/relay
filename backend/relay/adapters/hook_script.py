#!/usr/bin/env python3
"""relay event hook, copied verbatim into each work container.

This script runs inside the container as a Claude Code command hook
(``python3 hook.py claude``, event JSON on stdin) and as the Codex ``notify``
program (``python3 hook.py codex [--relay-session-id=ID] '<json>'``, event
JSON as the last argument). A relay-started Codex passes its relay id in the
argv, because the shared Codex daemon that runs ``notify`` does not inherit
the session's environment. A valid argv id wins over ``RELAY_SESSION_ID``.

Constraints, because it runs under the container's Python 3.11 and must also
stay compatible with Python 3.8:

- Standard library only.
- Never write to stdout. Claude reads a hook's stdout as a decision, so any
  text here could change agent behaviour. All errors are swallowed.
- Always exit 0. A non-zero exit from a Claude hook can block a tool call.
- Trim the payload so an inbox file stays small and never stores a full tool
  response or a long transcript.

The script writes one JSON file per event into
``~/.local/state/relay/inbox`` with an atomic ``os.replace``.
"""

import json
import os
import sys
import time

# tool_input keys worth keeping verbatim (short, safe metadata).
_KEEP_TOOL_INPUT = ("command", "description", "file_path", "path", "pattern", "url", "query")
# tool_input keys kept only as a short preview (potentially large content).
_PREVIEW_TOOL_INPUT = ("content", "new_string", "old_string", "prompt")
_PREVIEW_LEN = 200
_STRING_CAP = 2000
_LAST_MESSAGE_CAP = 4000
_MAX_DEPTH = 6
_RELAY_ID_FLAG = "--relay-session-id="
_HEX = "0123456789abcdef"


def _cap(value, limit):
    # type: (object, int) -> object
    if isinstance(value, str) and len(value) > limit:
        return value[:limit]
    return value


def _trim_tool_input(tool_input):
    # type: (object) -> object
    if not isinstance(tool_input, dict):
        return _trim_value(tool_input, 0)
    out = {}
    for key in _KEEP_TOOL_INPUT:
        if key in tool_input:
            out[key] = _cap(tool_input[key], _STRING_CAP)
    for key in _PREVIEW_TOOL_INPUT:
        if key in tool_input:
            val = tool_input[key]
            if isinstance(val, str):
                out[key] = val[:_PREVIEW_LEN]
    return out


def _trim_value(value, depth):
    # type: (object, int) -> object
    if depth > _MAX_DEPTH:
        return None
    if isinstance(value, str):
        return value[:_STRING_CAP]
    if isinstance(value, dict):
        return {str(k): _trim_value(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_trim_value(v, depth + 1) for v in value[:100]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:_STRING_CAP]


def _trim_payload(payload):
    # type: (dict) -> dict
    out = {}
    for key, value in payload.items():
        if key == "tool_input":
            out[key] = _trim_tool_input(value)
        elif key == "tool_response":
            # Never store a tool response: it can be huge and it is not needed.
            continue
        elif key == "last_assistant_message" or key == "last-assistant-message":
            out[key] = _cap(value, _LAST_MESSAGE_CAP) if isinstance(value, str) else _trim_value(value, 1)
        else:
            out[key] = _trim_value(value, 1)
    return out


def _read_claude_event():
    # type: () -> dict
    raw = sys.stdin.buffer.read()
    return json.loads(raw.decode("utf-8", "replace"))


def _read_codex_event(argv):
    # type: (list) -> dict
    raw = argv[-1]
    return json.loads(raw)


def _argv_relay_id(argv):
    # type: (list) -> object
    """The relay id given as ``--relay-session-id=<32 hex>`` before the payload."""
    for arg in argv[2:-1]:
        if isinstance(arg, str) and arg.startswith(_RELAY_ID_FLAG):
            value = arg[len(_RELAY_ID_FLAG):]
            if len(value) == 32 and all(ch in _HEX for ch in value):
                return value
    return None


def _inbox_dir():
    # type: () -> str
    home = os.path.expanduser("~")
    return os.path.join(home, ".local", "state", "relay", "inbox")


def _write_event(record):
    # type: (dict) -> None
    directory = _inbox_dir()
    try:
        os.makedirs(directory, 0o700)
    except OSError:
        pass
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    name = "{0}-{1:08x}.json".format(time.time_ns(), int.from_bytes(os.urandom(4), "big"))
    final = os.path.join(directory, name)
    tmp = os.path.join(directory, "." + name + ".tmp")
    data = json.dumps(record, separators=(",", ":")).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, final)


def _build_record(source, payload, argv_relay_id=None):
    # type: (str, dict, object) -> dict
    record = {
        "v": 1,
        "source": source,
        "ts": time.time(),
        "relay_session_id": argv_relay_id or os.environ.get("RELAY_SESSION_ID") or None,
        "relay_env": os.environ.get("RELAY_ENV") or None,
        "tmux": os.environ.get("TMUX") or None,
        "tmux_pane": os.environ.get("TMUX_PANE") or None,
        "payload": _trim_payload(payload),
    }
    if source == "codex":
        record["relay_id_from"] = "argv" if argv_relay_id else ("env" if record["relay_session_id"] else None)
    return record


def main(argv):
    # type: (list) -> int
    if len(argv) < 2:
        return 0
    source = argv[1]
    try:
        argv_relay_id = None
        if source == "claude":
            payload = _read_claude_event()
        elif source == "codex":
            payload = _read_codex_event(argv)
            argv_relay_id = _argv_relay_id(argv)
        else:
            return 0
        if not isinstance(payload, dict):
            return 0
        _write_event(_build_record(source, payload, argv_relay_id))
    except Exception:
        # Never surface an error: it could disturb the agent.
        return 0
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
