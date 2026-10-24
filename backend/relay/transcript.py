"""Read a Claude Code transcript into the dashboard conversation shape.

A transcript is a JSONL file the Claude CLI writes under
``<home>/.claude/projects/<slug>/<uuid>.jsonl``. relay reads it only to show a
conversation view; it never edits it.

Confinement rules (a session's ``transcript_path`` is a container path that a
hook reported, so it is not trusted):

- The env must map the container path to a host path.
- ``os.path.realpath`` of that host path must stay under
  ``realpath(host_home/.claude/projects)``.
- The suffix must be ``.jsonl``.
- relay reads at most the last 4 MiB of the file.

ConvItem shape (see ARCHITECTURE.md section 6)::

    {"id", "role": "user"|"assistant"|"tool", "text",
     "ts": epoch|null, "tool": {"name", "summary"}|null}
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from .config import EnvConfig

MAX_READ_BYTES = 4 * 1024 * 1024
MAX_ITEMS = 200
MAX_ITEM_BYTES = 32 * 1024
MAX_PAYLOAD_BYTES = 256 * 1024
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}$")
# Wrapper text the CLI injects around user turns; not real user input.
_WRAPPER_PREFIXES = ("<command-", "<system-reminder>", "<local-command-")


class TranscriptError(ValueError):
    """The transcript path is unusable or unsafe to read."""


def _projects_root(env: EnvConfig) -> str:
    host_projects = env.host_home / ".claude" / "projects"
    return os.path.realpath(str(host_projects))


def _resolve_transcript(env: EnvConfig, container_path: str, expected_session_id: str | None = None) -> str:
    if not isinstance(container_path, str) or not container_path.endswith(".jsonl") or "\x00" in container_path:
        raise TranscriptError("transcript path must be a .jsonl file")
    path = PurePosixPath(container_path)
    agent_id = expected_session_id if expected_session_id is not None else path.stem
    if not isinstance(agent_id, str) or not _ID_RE.fullmatch(agent_id) or path.name != f"{agent_id}.jsonl":
        raise TranscriptError("transcript file name does not match the session id")
    if ".." in path.parts or not path.is_relative_to(PurePosixPath(env.home, ".claude", "projects")):
        raise TranscriptError("transcript path is outside the projects directory")
    host_path = env.host_path(container_path)
    if host_path is None:
        raise TranscriptError("transcript path is outside the environment home")
    real = os.path.realpath(str(host_path))
    root = _projects_root(env)
    if not root.startswith(os.path.realpath(env.host_home) + os.sep):
        raise TranscriptError("projects directory escapes the environment home")
    rel = path.relative_to(PurePosixPath(env.home, ".claude", "projects"))
    if real != os.path.join(root, *rel.parts) or not real.startswith(root + os.sep):
        raise TranscriptError("transcript path escapes the projects directory")
    if not os.path.isfile(real):
        raise TranscriptError("transcript file not found")
    return real


def _read_tail_lines(path: str) -> list[str]:
    before = os.lstat(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    with os.fdopen(fd, "rb") as fh:
        opened = os.fstat(fh.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise TranscriptError("transcript file changed while opening")
        size = opened.st_size
        start = max(0, size - MAX_READ_BYTES)
        fh.seek(start)
        data = fh.read(size - start)
        current = os.stat(path, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino) or current.st_size < size:
            raise TranscriptError("transcript file changed while reading")
        if size > MAX_READ_BYTES:
            data = data.partition(b"\n")[2]  # drop the partial first line
    data = data[:data.rfind(b"\n") + 1]  # never parse an unfinished write
    text = data.decode("utf-8", "replace")
    return text.splitlines()


def _epoch(ts: Any) -> float | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        cleaned = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned).timestamp()
    except (ValueError, OverflowError):
        return None


def _is_wrapper_text(text: str) -> bool:
    stripped = text.lstrip()
    return any(stripped.startswith(p) for p in _WRAPPER_PREFIXES)


def _tool_summary(name: str, tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return name
    for key in ("command", "file_path", "path", "pattern", "url", "query", "description"):
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:200]
    return name


def _blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _string_content(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    return None


def bounded_items(items: list[dict[str, Any]], limit: int = MAX_ITEMS) -> list[dict[str, Any]]:
    """Bound the view after protecting the latest prompt and saved answer."""
    groups: list[tuple[dict[str, Any], list[str]]] = []
    previous: tuple[Any, ...] | None = None
    for source in items:
        key = (source.get("role"), re.sub(r"-\d+$", "", str(source.get("id") or "")), source.get("ts"), source.get("phase"))
        text = source.get("text") if isinstance(source.get("text"), str) else ""
        if groups and source.get("role") in ("user", "assistant") and key == previous:
            groups[-1][1].append(text)
        else:
            groups.append((dict(source), [text]))
        previous = key
    merged: list[dict[str, Any]] = []
    for item, parts in groups:
        item["text"] = "\n\n".join(parts)
        merged.append(item)
    users = [i for i, item in enumerate(merged) if item.get("role") == "user"]
    answers = [i for i, item in enumerate(merged) if item.get("role") == "assistant" and item.get("phase") != "commentary"]
    protected: list[int] = []
    if users:
        protected.append(users[-1])
    if answers:
        protected.append(answers[-1])
        anchor = next((i for i in reversed(users) if i < answers[-1]), None)
        if anchor is not None and anchor not in protected:
            protected.append(anchor)
    limit = max(1, min(int(limit), MAX_ITEMS))
    protected = protected[:limit]
    selected: dict[int, dict[str, Any]] = {}
    used = 2
    share = (MAX_PAYLOAD_BYTES - 2) // max(1, len(protected))
    order = protected + [i for i in reversed(range(len(merged))) if i not in protected]
    for index in order:
        if len(selected) >= limit:
            break
        budget = share if index in protected else MAX_PAYLOAD_BYTES - used
        item, size = _bounded_item(merged[index], budget)
        if size > budget or used + size > MAX_PAYLOAD_BYTES:
            continue
        selected[index] = item
        used += size
    return [selected[index] for index in sorted(selected)]


def _bounded_item(source: dict[str, Any], budget: int) -> tuple[dict[str, Any], int]:
    item = dict(source)
    item["id"] = str(item.get("id") or "")[:160]
    data = str(item.get("text") or "").encode("utf-8")
    tool = item.get("tool")
    if isinstance(tool, dict):
        item["tool"] = {key: str(tool.get(key) or "")[:200] for key in ("name", "summary")}
    def fit(cap: int) -> int:
        item["text"] = data[:cap].decode("utf-8", "ignore")
        if len(data) > cap:
            item["text"] += "\n[… message shortened]"
        return len(json.dumps(item, ensure_ascii=True).encode("utf-8")) + 1
    cap = min(len(data), MAX_ITEM_BYTES)
    size = fit(cap)
    if size > budget:
        low, high = 0, cap
        while low < high:
            middle = (low + high + 1) // 2
            if fit(middle) <= budget:
                low = middle
            else:
                high = middle - 1
        size = fit(low)
    return item, size


def read_conversation(
    env: EnvConfig, container_path: str, *, limit: int = 200, expected_session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Parse a Claude transcript into a list of ConvItem dicts (oldest first)."""
    real = _resolve_transcript(env, container_path, expected_session_id)
    agent_id = expected_session_id if expected_session_id is not None else PurePosixPath(container_path).stem
    limit = max(1, min(int(limit), MAX_ITEMS))
    items: list[dict[str, Any]] = []
    counter = 0
    for line in _read_tail_lines(real):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (TypeError, ValueError, RecursionError):
            continue
        if not isinstance(row, dict):
            continue
        row_type = row.get("type")
        if row_type not in ("user", "assistant"):
            continue
        if row.get("isMeta") or row.get("isSidechain"):
            continue
        message = row.get("message")
        if not isinstance(message, dict):
            continue
        if row.get("sessionId") != agent_id:
            raise TranscriptError("transcript message does not match the session id")
        ts = _epoch(row.get("timestamp"))
        uuid = row.get("uuid")
        base_id = uuid[:128] if isinstance(uuid, str) and uuid else "row-" + hashlib.sha256(line.encode("utf-8")).hexdigest()[:32]

        content = message.get("content")
        text_str = _string_content(content)
        if text_str is not None:
            if row_type == "user" and _is_wrapper_text(text_str):
                continue
            cleaned = text_str.strip()
            if cleaned:
                counter += 1
                items.append(
                    {
                        "id": f"{base_id}-0",
                        "role": row_type,
                        "text": cleaned,
                        "ts": ts,
                        "tool": None,
                    }
                )
            continue

        for index, block in enumerate(_blocks(content)):
            btype = block.get("type")
            if btype == "text":
                text = block.get("text")
                if not isinstance(text, str):
                    continue
                if row_type == "user" and _is_wrapper_text(text):
                    continue
                cleaned = text.strip()
                if not cleaned:
                    continue
                counter += 1
                items.append(
                    {
                        "id": f"{base_id}-{index}",
                        "role": row_type,
                        "text": cleaned,
                        "ts": ts,
                        "tool": None,
                    }
                )
            elif btype == "tool_use":
                name = block.get("name")
                if not isinstance(name, str) or not name:
                    continue
                summary = _tool_summary(name, block.get("input"))
                counter += 1
                items.append(
                    {
                        "id": f"{base_id}-{index}",
                        "role": "tool",
                        "text": "",
                        "ts": ts,
                        "tool": {"name": name, "summary": summary},
                    }
                )
            # tool_result and thinking blocks are skipped.
    return bounded_items(items, limit)
