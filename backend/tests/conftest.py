"""Shared fixtures. Owned by the foundation; builders add fixtures in their own test files."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fakes import FakePodman  # noqa: E402

from relay.config import EnvConfig, Settings  # noqa: E402
from relay.db import Database  # noqa: E402
from relay.runtime import Runtime, build_runtime  # noqa: E402


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    envs = {
        "work1": EnvConfig(id="work1", label="Work 1", container="job-work1", host_home=tmp_path / "work1"),
        "work2": EnvConfig(id="work2", label="Work 2", container="job-work2", host_home=tmp_path / "work2"),
    }
    for env in envs.values():
        env.host_home.mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    return Settings(state_dir=state, envs=envs, frontend_dist=tmp_path / "dist", browser_executable=None)


@pytest.fixture
def fake_podman() -> FakePodman:
    return FakePodman()


@pytest.fixture
def rt(settings: Settings, fake_podman: FakePodman) -> Runtime:
    runtime = build_runtime(settings, podman=fake_podman, db=Database(settings.db_path))
    yield runtime
    runtime.db.close()
