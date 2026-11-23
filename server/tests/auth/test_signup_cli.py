"""SRV-AUTH sign-up at the process level: the startup refusal line and bootstrap-link's refusal.

@trace FR-AUTH-24 INV-AUTH-13
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import anyio
from auth_helpers import cli_env, config_text, ready_engine, server_cmd, settings_for, write_config
from conftest import Pg
from signup_helpers import insert_pending


def _env(pg: Pg, tmp_path: Path, users: tuple[tuple[str, str], ...]) -> dict[str, str]:
    cfg = write_config(tmp_path / "cfg", config_text(users=users))
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "db-password").write_text(pg.password, encoding="utf-8")
    run = tmp_path / "run"
    run.mkdir()
    return cli_env(pg, cfg, secrets, run)


def _seed(pg: Pg, tmp_path: Path) -> None:
    async def run() -> None:
        engine = await ready_engine(pg, settings_for(tmp_path / "seed", users=(("ivan", "owner"),)))
        await insert_pending(engine, "maria", state="active")
        await engine.dispose()

    anyio.run(run)


def test_api_stops_with_the_colliding_handle(pg: Pg, tmp_path: Path) -> None:
    _seed(pg, tmp_path)
    env = _env(pg, tmp_path, (("ivan", "owner"), ("maria", "learner")))
    done = subprocess.run(
        server_cmd("--role", "api", "--bind", "127.0.0.1:0"), env=env, capture_output=True, text=True, timeout=120,
        check=False,
    )
    assert done.returncode == 1
    assert done.stderr == "reader-server: handle_taken_by_signup maria\n"
    assert done.stdout == ""


def test_bootstrap_link_refuses_a_signup_user(pg: Pg, tmp_path: Path) -> None:
    _seed(pg, tmp_path)
    env = _env(pg, tmp_path, (("ivan", "owner"),))
    done = subprocess.run(
        server_cmd("bootstrap-link", "--handle", "maria"), env=env, capture_output=True, text=True, timeout=120,
        check=False,
    )
    assert done.returncode == 1
    assert done.stderr == "reader-server: signup_user\n"
    assert done.stdout == ""
