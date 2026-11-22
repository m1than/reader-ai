"""Fixtures and helpers for the SRV-EVENTS tests (spec/modules/SRV-EVENTS.yaml).

@trace FR-EVENTS-1

[db] tests run against a Postgres server from the image OPS-DEPLOY pins (testcontainers), one fresh
database per test, migrated to all heads through `reader.db.migrate`. They skip when no Docker daemon is
reachable.

The modules under test (`reader.jobs`, and `reader.db` with the jobs migrations) are reached only through
`require_jobs()` and `jobs_env()`, which test bodies call: in the Red phase each test fails on the
assertion in `require_jobs()`, inside its own body.

A test kind, `test.effect`, records its effect as one row in `t_effects` inside the job's transaction, so
an effect count per key is a database fact (INV-EVENTS-2). Its `mode` argument (not part of the key)
selects the behaviour: ok, transient, permanent; `sleep_s` delays it.
"""

from __future__ import annotations

import asyncio
import importlib.util
import secrets
import shutil
import subprocess
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

REPO = Path(__file__).resolve().parents[3]
SPEC = REPO / "spec" / "modules" / "SRV-EVENTS.yaml"
FIXTURES = REPO / "spec" / "fixtures" / "SRV-EVENTS"
# The image OPS-DEPLOY pins (deploy/images/postgres.Dockerfile): postgres:18-trixie, linux/arm64/v8 present.
PG_IMAGE = "postgres:18-trixie@sha256:eb63372c1e05c755f55fa30cbd07aecc443676fc6c0fa90b9002d01537841e1a"
KIND = "test.effect"


def load_yaml(path: Path) -> Any:
    return YAML(typ="safe").load(path.read_text(encoding="utf-8"))


def require_jobs() -> Any:
    # reader.jobs is a directory before Green can import it (stale caches), so probe a real module.
    for module in ("reader.jobs.queue", "reader.jobs.worker", "reader.jobs.states", "reader.jobs.logfilter"):
        assert importlib.util.find_spec(module) is not None, f"{module} does not exist yet"
    import reader.jobs as jobs

    return jobs


def _daemon_up() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0  # noqa: S607


DOCKER = _daemon_up()


@dataclass(frozen=True)
class Pg:
    host: str
    port: int
    user: str
    password: str
    database: str


@pytest.fixture(scope="session")
def pg_container() -> Iterator[tuple[Pg, Any]]:
    if not DOCKER:
        pytest.skip("docker daemon not reachable (Postgres via testcontainers)")
    from testcontainers.community.postgres import PostgresContainer

    password = "pw-" + secrets.token_hex(8)
    with PostgresContainer(
        PG_IMAGE, username="postgres", password=password, dbname="postgres", driver=None
    ) as c:
        host, port = c.get_container_host_ip(), int(c.get_exposed_port(5432))
        yield Pg(host, port, "postgres", password, "postgres"), c


@pytest.fixture
def pg(pg_container: tuple[Pg, Any]) -> Pg:
    server, container = pg_container
    name = "j_" + secrets.token_hex(6)
    code, out = container.exec(["psql", "-U", "postgres", "-c", f"CREATE DATABASE {name}"])
    assert code == 0, out
    return Pg(server.host, server.port, server.user, server.password, name)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Transient(Exception):
    """The test kind's transient failure."""


# A secret value the handler holds from outside its payload (as a provider token would be):
# `message: "@secret"` makes the handler's exception message carry it.
HANDLER_SECRET: list[str] = [""]
# Every handler call, by key: an effect outside the database (as a provider call would be). FR-EVENTS-4's
# skip is observable only here, because FR-EVENTS-13's guard would roll a second database effect back.
HANDLER_RUNS: list[str] = []
_EFFECT_SQL = "INSERT INTO t_effects (key, job_id) VALUES (:k, :j)"


def effect_kind(max_attempts: int = 3, timeout_s: float = 30.0) -> Any:
    from pydantic import BaseModel, ConfigDict
    from sqlalchemy import text

    jobs = require_jobs()

    class Args(BaseModel):
        model_config = ConfigDict(extra="forbid")
        name: str
        mode: str = "ok"
        sleep_s: float = 0.0
        message: str = ""

    async def handler(args: Args, conn: Any, ctx: Any) -> None:
        HANDLER_RUNS.append(ctx.key)
        await conn.execute(text(_EFFECT_SQL), {"k": ctx.key, "j": ctx.job_id})
        if args.sleep_s:
            await asyncio.sleep(args.sleep_s)
        message = HANDLER_SECRET[0] if args.message == "@secret" else args.message
        if args.mode == "transient":
            raise Transient(message)
        if args.mode == "permanent":
            raise RuntimeError(message)

    return jobs.JobKind(
        name=KIND,
        queue="test",
        args=Args,
        key=("name",),
        handler=handler,
        max_attempts=max_attempts,
        transient=(Transient,),
        timeout_s=timeout_s,
    )


@dataclass
class Env:
    pg: Pg
    engine: Any
    app: Any
    jobs: Any
    kind: Any

    async def scalar(self, sql: str, **params: Any) -> Any:
        from sqlalchemy import text

        async with self.engine.connect() as conn:
            return (await conn.execute(text(sql), params)).scalar()

    async def rows(self, sql: str, **params: Any) -> list[Any]:
        from sqlalchemy import text

        async with self.engine.connect() as conn:
            return list((await conn.execute(text(sql), params)).all())

    async def execute(self, sql: str, **params: Any) -> None:
        from sqlalchemy import text

        async with self.engine.begin() as conn:
            await conn.execute(text(sql), params)

    def key(self, name: str) -> str:
        return str(require_jobs().idempotency_key(self.kind, {"name": name}))

    def runs(self, name: str) -> int:
        return HANDLER_RUNS.count(self.key(name))

    async def effects(self, name: str) -> int:
        return int(await self.scalar("SELECT count(*) FROM t_effects WHERE key = :k", k=self.key(name)))

    async def key_row(self, name: str) -> Any:
        rows = await self.rows(
            "SELECT attempts, outcome, error_code FROM job_keys WHERE key = :k", k=self.key(name)
        )
        return rows[0] if rows else None

    async def job_statuses(self, name: str) -> list[str]:
        rows = await self.rows(
            "SELECT status::text FROM procrastinate_jobs WHERE lock = :k ORDER BY id", k=self.key(name)
        )
        return [r[0] for r in rows]

    async def work(self) -> None:
        """Run a worker until the queues are empty (`wait=False`)."""
        await self.app.run_worker_async(
            queues=self.jobs.queues,
            wait=False,
            concurrency=1,
            install_signal_handlers=False,
            listen_notify=False,
        )

    async def stall_next(self) -> Any:
        """Fetch the next job under a registered worker, as a worker would, then make that worker stale."""
        worker_id = await self.app.job_manager.register_worker()
        job = await self.app.job_manager.fetch_job(queues=None, worker_id=worker_id)
        assert job is not None
        await self.execute(
            "UPDATE procrastinate_workers SET last_heartbeat = now() - interval '1 hour' WHERE id = :w",
            w=worker_id,
        )
        return job

    async def age_keys(self) -> None:
        await self.execute("UPDATE job_keys SET updated_at = now() - interval '1 hour'")


def target(pg: Pg) -> Any:
    from reader.config import Secret
    from reader.db import DbTarget

    return DbTarget(pg.host, pg.port, pg.user, pg.database, Secret(pg.password))


@asynccontextmanager
async def jobs_env(
    pg: Pg,
    *,
    kinds: list[Any] | None = None,
    secret_values: tuple[str, ...] = (),
    max_attempts: int = 3,
    timeout_s: float = 30.0,
) -> AsyncIterator[Env]:
    jobs = require_jobs()
    from reader.db import make_engine, migrate
    from reader.jobs.queue import Jobs
    from reader.jobs.worker import make_app

    kind = effect_kind(max_attempts=max_attempts, timeout_s=timeout_s)
    engine = make_engine(target(pg))
    await migrate(engine)
    env_app = make_app(target(pg))
    await env_app.open_async()
    try:
        catalog = jobs.Catalog(kinds if kinds is not None else [kind])
        runtime = Jobs(engine, env_app, catalog, secret_values, retry_delay=lambda _attempts: 0)
        env = Env(pg, engine, env_app, runtime, kind)
        await env.execute("CREATE TABLE t_effects (key text NOT NULL, job_id bigint NOT NULL)")
        yield env
    finally:
        await env_app.close_async()
        await engine.dispose()
