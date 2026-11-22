"""Helpers the SRV-AUTH test bodies call. Imports of the modules under test happen inside these functions."""

from __future__ import annotations

import os
import sys
import textwrap
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from conftest import Pg
from softauthn import SoftAuthenticator, b64url, unb64url

ORIGIN = "https://localhost"
REPO = Path(__file__).resolve().parents[3]
SPEC = REPO / "spec" / "modules" / "SRV-AUTH.yaml"
FIXTURES = REPO / "spec" / "fixtures" / "SRV-AUTH"


def config_text(
    url: str = ORIGIN,
    users: tuple[tuple[str, str], ...] = (("ivan", "owner"), ("live-check", "learner")),
    sharing: str = "{}",
) -> str:
    entries = "".join(f"\n  - {{ handle: {h}, role: {r} }}" for h, r in users) if users else " []"
    return (
        f'version: 1\ninstance: {{ url: "{url}", data_dir: ./data }}\nusers:{entries}\nsharing: {sharing}\n'
    )


def write_config(directory: Path, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "reader.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def settings_for(tmp: Path, **kwargs: Any) -> Any:
    from reader.auth.policy import settings_from_config

    from reader.config import load_config

    return settings_from_config(load_config(write_config(tmp, config_text(**kwargs))))


def target(pg: Pg, password: str | None = None) -> Any:
    from reader.db import DbTarget

    from reader.config import Secret

    return DbTarget(
        pg.host, pg.port, pg.user, pg.database, Secret(pg.password if password is None else password)
    )


async def ready_engine(pg: Pg, settings: Any) -> Any:
    """Migrated database with the configured users (FR-AUTH-1 startup without Uvicorn)."""
    from reader.auth.store import prepare
    from reader.db import make_engine, migrate

    engine = make_engine(target(pg))
    await migrate(engine)
    await prepare(engine, settings)
    return engine


@asynccontextmanager
async def client(settings: Any, engine: Any) -> AsyncIterator[httpx.AsyncClient]:
    from reader.api.app import create_app

    app = create_app(settings, engine, run_cleanup_task=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN, headers={"Origin": ORIGIN}) as c:
        yield c


async def scalar(engine: Any, sql: str, **params: Any) -> Any:
    from sqlalchemy import text

    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar()


async def execute(engine: Any, sql: str, **params: Any) -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text(sql), params)


async def bootstrap_token(engine: Any, settings: Any, handle: str | None = None) -> str:
    from reader.auth.store import create_bootstrap_token

    return str(await create_bootstrap_token(engine, settings, handle))


async def bootstrap(
    c: httpx.AsyncClient, token: str, auth: SoftAuthenticator, **mutation: Any
) -> tuple[httpx.Response, dict[str, Any]]:
    """Run options and verify; returns the verify response and the options."""
    options = await c.post("/api/auth/bootstrap/options", json={"token": token})
    assert options.status_code == 200, options.text
    opts = options.json()
    credential = auth.register(challenge=unb64url(opts["challenge"]), **mutation)
    return await c.post("/api/auth/bootstrap/verify", json=credential), opts


async def signin(
    c: httpx.AsyncClient, auth: SoftAuthenticator, user_handle: bytes | None, **mutation: Any
) -> httpx.Response:
    options = await c.post("/api/auth/signin/options", json={})
    assert options.status_code == 200, options.text
    challenge = unb64url(options.json()["challenge"])
    return await c.post(
        "/api/auth/signin/verify",
        json=auth.assertion(challenge=challenge, user_handle=user_handle, **mutation),
    )


async def owner_with_passkey(
    c: httpx.AsyncClient, engine: Any, settings: Any, auth: SoftAuthenticator | None = None
) -> tuple[SoftAuthenticator, bytes]:
    auth = auth or SoftAuthenticator.random()
    response, opts = await bootstrap(c, await bootstrap_token(engine, settings), auth)
    assert response.status_code == 200, response.text
    return auth, unb64url(opts["user"]["id"])


def set_cookies(response: httpx.Response) -> list[str]:
    return response.headers.get_list("set-cookie")


def cli_env(pg: Pg, config: Path, secrets_root: Path, run_dir: Path, **extra: str) -> dict[str, str]:
    """Environment for `reader-server` subprocesses: the documented test hooks, no inherited secrets."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(run_dir),
        "READER_CONFIG": str(config),
        "READER_SECRETS_ROOT": str(secrets_root),
        "READER_RUN_DIR": str(run_dir),
        "READER_DB_HOST": pg.host,
        "READER_DB_PORT": str(pg.port),
        "READER_DB_USER": pg.user,
        "READER_DB_NAME": pg.database,
    }
    env.update(extra)
    return env


def server_cmd(*args: str) -> list[str]:
    return [sys.executable, "-m", "reader.serve", *args]


__all__ = ["b64url", "unb64url"]
