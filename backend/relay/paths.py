"""Default filesystem locations for relay state and configuration.

Every path can be overridden, which keeps tests away from the real state:

- ``RELAY_STATE_DIR``: database, token, artifacts, logs (default ``~/.local/state/relay``)
- ``RELAY_CONFIG``: TOML config file (default ``~/.config/relay/config.toml``)
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"


def default_state_dir() -> Path:
    override = os.environ.get("RELAY_STATE_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "relay"


def default_config_path() -> Path:
    override = os.environ.get("RELAY_CONFIG")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "relay" / "config.toml"


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (and parents) and restrict it to the current user."""
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def write_private_file(path: Path, data: bytes | str) -> None:
    """Atomically write a file readable only by the current user."""
    raw = data.encode() if isinstance(data, str) else data
    ensure_private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, raw)
    finally:
        os.close(fd)
    os.replace(tmp, path)
