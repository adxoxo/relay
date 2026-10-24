"""Settings for the relay service.

Configuration is optional. Without a config file, relay uses the two known
work environments (``job-work1`` and ``job-work2``) with their standard mounts.

Example ``~/.config/relay/config.toml``::

    [server]
    host = "127.0.0.1"
    port = 8765
    public_origins = ["https://laptop.example.ts.net"]
    tailscale_hostname = "laptop.example.ts.net"
    tailscale_logins = ["adam@example.com"]

    [envs.work1]
    label = "Work 1"
    container = "job-work1"
    host_home = "/home/adyu/Work/work1"

    [files]
    edit_max_bytes = 10000000     # full editor limit
    paged_max_bytes = 50000000    # paged viewer limit; larger files are download only

    [previews]
    docker_socket = "/run/docker.sock"   # "" turns Docker discovery off

    # One trusted gateway listener per approved app. Tailscale Serve forwards
    # the external HTTPS port to listen_port (a separate, approved step).
    [[previews.gateways]]
    listen_port = 18501
    env = "work1"
    target_port = 15173           # the app's loopback port on the laptop
    public_origin = "https://laptop.example.ts.net:18501"
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

from . import paths

ENV_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class ConfigError(ValueError):
    """The configuration file is invalid."""


@dataclass(frozen=True)
class EnvConfig:
    """One work environment (a Podman container with a bind-mounted home)."""

    id: str
    label: str
    container: str
    host_home: Path
    home: str = "/home/node"
    projects_root: str = "/home/node/projects"
    exec_user: str = "1000:1000"
    tmux_sockets: tuple[str, ...] = ("/tmp/tmux-1000/default",)
    previews_socket: str = "/tmp/tmux-1000/relay-previews"

    # Container-side locations that relay provisions (all under the bind-mounted home).
    @property
    def share_dir(self) -> str:
        return f"{self.home}/.local/share/relay"

    @property
    def inbox_dir(self) -> str:
        return f"{self.home}/.local/state/relay/inbox"

    @property
    def hook_script(self) -> str:
        return f"{self.share_dir}/hook.py"

    @property
    def claude_settings(self) -> str:
        return f"{self.share_dir}/claude-settings.json"

    def host_path(self, container_path: str) -> Path | None:
        """Map a container path under ``home`` to the host path, else ``None``.

        The result is only lexically mapped. Callers that open the file must
        still resolve symlinks and confirm the result stays under ``host_home``.
        """
        p = PurePosixPath(container_path)
        home = PurePosixPath(self.home)
        if not p.is_absolute() or ".." in p.parts:
            return None
        try:
            rel = p.relative_to(home)
        except ValueError:
            return None
        return self.host_home.joinpath(*rel.parts) if rel.parts else self.host_home

    @property
    def host_inbox_dir(self) -> Path:
        mapped = self.host_path(self.inbox_dir)
        assert mapped is not None
        return mapped

    @property
    def host_share_dir(self) -> Path:
        mapped = self.host_path(self.share_dir)
        assert mapped is not None
        return mapped

    @property
    def host_codex_config(self) -> Path:
        """Host path of the env's global Codex config (``~/.codex/config.toml``)."""
        return self.host_home / ".codex" / "config.toml"


@dataclass(frozen=True)
class GatewayConfig:
    """One trusted preview gateway: a loopback listener in the relay process.

    It forwards to exactly one loopback endpoint (``127.0.0.1:target_port``)
    after a relay session check. ``public_origin`` is the external origin that
    Tailscale Serve maps to ``listen_port``; relay shows a phone link only
    after it proves that this origin reaches this listener.
    """

    listen_port: int
    env: str
    target_port: int
    public_origin: str | None = None

    @property
    def local_origin(self) -> str:
        return f"http://127.0.0.1:{self.listen_port}"


@dataclass(frozen=True)
class Settings:
    state_dir: Path
    envs: dict[str, EnvConfig]
    host: str = "127.0.0.1"
    port: int = 8765
    public_origins: tuple[str, ...] = ()
    tailscale_hostname: str | None = None
    tailscale_logins: tuple[str, ...] = ()
    poll_interval: float = 3.0
    inbox_interval: float = 1.0
    cookie_max_age: int = 30 * 24 * 3600
    frontend_dist: Path = field(default_factory=lambda: paths.FRONTEND_DIST)
    podman_binary: str = "podman"
    browser_executable: str | None = None  # Chromium-compatible browser for checks
    files_edit_max_bytes: int = 10_000_000
    files_paged_max_bytes: int = 50_000_000
    docker_socket: str | None = "/run/docker.sock"
    preview_gateways: tuple[GatewayConfig, ...] = ()

    @property
    def db_path(self) -> Path:
        return self.state_dir / "relay.db"

    @property
    def token_path(self) -> Path:
        return self.state_dir / "token"

    @property
    def artifacts_dir(self) -> Path:
        return self.state_dir / "artifacts"

    @property
    def local_origins(self) -> tuple[str, ...]:
        return (
            f"http://127.0.0.1:{self.port}",
            f"http://localhost:{self.port}",
        )

    @property
    def allowed_origins(self) -> tuple[str, ...]:
        """Browser origins accepted for state changes and WebSockets."""
        return self.local_origins + tuple(o.rstrip("/") for o in self.public_origins)

    def env(self, env_id: str) -> EnvConfig:
        try:
            return self.envs[env_id]
        except KeyError:
            raise KeyError(f"unknown environment: {env_id}") from None

    def with_overrides(self, **changes: Any) -> "Settings":
        return replace(self, **changes)


def default_envs(home: Path | None = None) -> dict[str, EnvConfig]:
    base = (home or Path.home()) / "Work"
    return {
        "work1": EnvConfig(id="work1", label="Work 1", container="job-work1", host_home=base / "work1"),
        "work2": EnvConfig(id="work2", label="Work 2", container="job-work2", host_home=base / "work2"),
    }


def _default_browser() -> str | None:
    for candidate in ("/opt/brave.com/brave/brave", "/usr/bin/chromium", "/usr/bin/google-chrome"):
        if Path(candidate).exists():
            return candidate
    return None


def _as_tuple(value: Any, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{key} must be a list of strings")
    return tuple(value)


def _parse_env(env_id: str, raw: dict[str, Any], defaults: EnvConfig | None) -> EnvConfig:
    if not ENV_ID_RE.match(env_id):
        raise ConfigError(f"invalid environment id: {env_id!r}")
    if not isinstance(raw, dict):
        raise ConfigError(f"envs.{env_id} must be a table")
    base: dict[str, Any] = {}
    if defaults is not None:
        base = {
            "label": defaults.label,
            "container": defaults.container,
            "host_home": defaults.host_home,
            "home": defaults.home,
            "projects_root": defaults.projects_root,
            "exec_user": defaults.exec_user,
            "tmux_sockets": defaults.tmux_sockets,
            "previews_socket": defaults.previews_socket,
        }
    for key in ("label", "container", "home", "projects_root", "exec_user", "previews_socket"):
        if key in raw:
            if not isinstance(raw[key], str) or not raw[key]:
                raise ConfigError(f"envs.{env_id}.{key} must be a non-empty string")
            base[key] = raw[key]
    if "host_home" in raw:
        base["host_home"] = Path(str(raw["host_home"])).expanduser()
    if "tmux_sockets" in raw:
        base["tmux_sockets"] = _as_tuple(raw["tmux_sockets"], f"envs.{env_id}.tmux_sockets")
    missing = [k for k in ("label", "container", "host_home") if k not in base]
    if missing:
        raise ConfigError(f"envs.{env_id} is missing {', '.join(missing)}")
    env = EnvConfig(id=env_id, **base)
    for p in (env.home, env.projects_root, *env.tmux_sockets, env.previews_socket):
        if not p.startswith("/"):
            raise ConfigError(f"envs.{env_id}: container paths must be absolute: {p!r}")
    if not env.projects_root.startswith(env.home.rstrip("/") + "/") and env.projects_root != env.home:
        raise ConfigError(f"envs.{env_id}: projects_root must be under home")
    return env


def load_settings(path: Path | None = None, **overrides: Any) -> Settings:
    """Load settings from TOML (if present) and apply keyword overrides."""
    cfg_path = path or paths.default_config_path()
    data: dict[str, Any] = {}
    if cfg_path.exists():
        try:
            data = tomllib.loads(cfg_path.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{cfg_path}: {exc}") from exc

    server = data.get("server", {})
    if not isinstance(server, dict):
        raise ConfigError("[server] must be a table")

    defaults = default_envs()
    envs_raw = data.get("envs")
    if envs_raw is None:
        envs = defaults
    else:
        if not isinstance(envs_raw, dict) or not envs_raw:
            raise ConfigError("[envs] must contain at least one environment")
        envs = {eid: _parse_env(eid, raw, defaults.get(eid)) for eid, raw in envs_raw.items()}

    kwargs: dict[str, Any] = {
        "state_dir": Path(server["state_dir"]).expanduser() if "state_dir" in server else paths.default_state_dir(),
        "envs": envs,
        "browser_executable": _default_browser(),
    }
    if "host" in server:
        kwargs["host"] = str(server["host"])
    if "port" in server:
        kwargs["port"] = int(server["port"])
    if "public_origins" in server:
        kwargs["public_origins"] = _as_tuple(server["public_origins"], "server.public_origins")
    if "tailscale_hostname" in server:
        kwargs["tailscale_hostname"] = str(server["tailscale_hostname"]) or None
    if "tailscale_logins" in server:
        kwargs["tailscale_logins"] = _as_tuple(server["tailscale_logins"], "server.tailscale_logins")
    if "poll_interval" in server:
        kwargs["poll_interval"] = max(1.0, float(server["poll_interval"]))
    if "browser_executable" in server:
        kwargs["browser_executable"] = str(server["browser_executable"]) or None

    files = data.get("files", {})
    if not isinstance(files, dict):
        raise ConfigError("[files] must be a table")
    for key, attr in (("edit_max_bytes", "files_edit_max_bytes"), ("paged_max_bytes", "files_paged_max_bytes")):
        if key in files:
            value = files[key]
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ConfigError(f"files.{key} must be a positive integer")
            kwargs[attr] = value

    previews_cfg = data.get("previews", {})
    if not isinstance(previews_cfg, dict):
        raise ConfigError("[previews] must be a table")
    if "docker_socket" in previews_cfg:
        kwargs["docker_socket"] = str(previews_cfg["docker_socket"]) or None
    if "gateways" in previews_cfg:
        raw_gateways = previews_cfg["gateways"]
        if not isinstance(raw_gateways, list):
            raise ConfigError("[[previews.gateways]] must be an array of tables")
        kwargs["preview_gateways"] = tuple(_parse_gateway(i, g) for i, g in enumerate(raw_gateways))

    kwargs.update(overrides)
    settings = Settings(**kwargs)
    if settings.host not in ("127.0.0.1", "localhost", "::1"):
        # Tailscale Serve proxies to loopback. Refuse to listen on other
        # interfaces so the terminal bridge is never exposed on the LAN.
        raise ConfigError("server.host must be a loopback address")
    if settings.files_edit_max_bytes > settings.files_paged_max_bytes:
        raise ConfigError("files.edit_max_bytes must not exceed files.paged_max_bytes")
    validate_gateways(settings)
    return settings


def _parse_gateway(index: int, raw: Any) -> GatewayConfig:
    where = f"previews.gateways[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a table")
    unknown = set(raw) - {"listen_port", "env", "target_port", "public_origin"}
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")
    for key in ("listen_port", "target_port"):
        value = raw.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or not (1024 <= value <= 65535):
            raise ConfigError(f"{where}.{key} must be a port between 1024 and 65535")
    env = raw.get("env")
    if not isinstance(env, str) or not env:
        raise ConfigError(f"{where}.env must name an environment")
    origin = raw.get("public_origin")
    if origin is not None:
        if not isinstance(origin, str):
            raise ConfigError(f"{where}.public_origin must be a string")
        origin = origin.rstrip("/") or None
    return GatewayConfig(listen_port=raw["listen_port"], env=env, target_port=raw["target_port"], public_origin=origin)


def validate_gateways(settings: Settings) -> None:
    """Reject a gateway set that could forward to relay itself or overlap."""
    from urllib.parse import urlsplit

    listen_ports: set[int] = set()
    for gw in settings.preview_gateways:
        if gw.env not in settings.envs:
            raise ConfigError(f"gateway {gw.listen_port}: unknown environment {gw.env!r}")
        if gw.listen_port == settings.port or gw.target_port == settings.port:
            raise ConfigError(f"gateway {gw.listen_port}: must not use relay's own port")
        if gw.listen_port == gw.target_port:
            raise ConfigError(f"gateway {gw.listen_port}: listen_port and target_port must differ")
        if gw.listen_port in listen_ports:
            raise ConfigError(f"gateway {gw.listen_port}: duplicate listen_port")
        listen_ports.add(gw.listen_port)
        if gw.public_origin is not None:
            parts = urlsplit(gw.public_origin)
            if parts.scheme != "https" or not parts.hostname or parts.path not in ("", "/") or parts.query:
                raise ConfigError(f"gateway {gw.listen_port}: public_origin must be an https origin")
            if gw.public_origin in settings.allowed_origins:
                raise ConfigError(f"gateway {gw.listen_port}: public_origin must differ from relay's own origins")
    for gw in settings.preview_gateways:
        if gw.target_port in listen_ports:
            raise ConfigError(f"gateway {gw.listen_port}: target_port is another gateway's listen_port")
