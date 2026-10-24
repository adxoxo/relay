"""Poll each environment's inbox directory and process event files.

The hook script writes one JSON file per event into the env's host inbox
directory. relay reads them in name order (the name starts with ``time_ns``),
hands each to ``adapters.handle_event``, and deletes it. This is a one-way,
network-free path, so work1 (slirp) and work2 (host network) behave the same.

Safety rules for every file:

- Only names matching ``^\\d+-[0-9a-f]{8}\\.json$``.
- ``lstat`` the file and require a regular file (never follow a symlink).
- Size must be 256 KiB or less.
- At most 500 files per tick.
- Delete every file after handling, including an invalid one, so a bad file
  never blocks the queue.
- Never let one file crash the loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
from typing import TYPE_CHECKING

from . import adapters

if TYPE_CHECKING:  # pragma: no cover
    from .runtime import Runtime

_NAME_RE = re.compile(r"^\d+-[0-9a-f]{8}\.json$")
_MAX_SIZE = 256 * 1024
_MAX_PER_TICK = 500


def _delete(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


async def process_once(rt: "Runtime", env_id: str) -> int:
    """Process one tick of the env's inbox. Returns the count handled."""
    try:
        env = rt.settings.env(env_id)
    except KeyError:
        return 0
    directory = str(env.host_inbox_dir)
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return 0

    handled = 0
    for name in names:
        if handled >= _MAX_PER_TICK:
            break
        if not _NAME_RE.match(name):
            continue
        path = os.path.join(directory, name)
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode):
            _delete(path)
            continue
        if info.st_size > _MAX_SIZE:
            _delete(path)
            handled += 1
            continue
        try:
            with open(path, "rb") as fh:
                raw = fh.read(_MAX_SIZE + 1)
            event = json.loads(raw.decode("utf-8", "replace"))
            if isinstance(event, dict):
                await adapters.handle_event(rt, env_id, event)
        except Exception:
            # A single bad file must not stop the loop.
            pass
        finally:
            _delete(path)
        handled += 1
    return handled


async def run_forever(rt: "Runtime") -> None:
    """Poll every env's inbox forever, at ``settings.inbox_interval`` seconds."""
    interval = max(0.1, float(rt.settings.inbox_interval))
    while True:
        for env_id in list(rt.settings.envs):
            try:
                await process_once(rt, env_id)
            except Exception:
                pass
        await asyncio.sleep(interval)
