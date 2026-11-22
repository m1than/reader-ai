"""Database engine and migrations (SRV-AUTH FR-AUTH-1; ADR-0006).

The password reaches asyncpg as a separate connect argument, so no URL or DSN string ever contains it.
Migrations run through the app's asyncpg engine (`run_sync`) under a two-key advisory lock in this module's
namespace, to all heads of the history whose directories `VERSION_LOCATIONS` declares (Alembic does not glob).
"""

from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from reader.config import Secret

_HERE: Final = Path(__file__).resolve().parent
SCRIPT_LOCATION: Final = _HERE / "migrations"


def _procrastinate_versions() -> Path:
    """procrastinate's shipped Alembic revisions (branch `procrastinate`), in the installed package."""
    spec = importlib.util.find_spec("procrastinate")
    if spec is None or spec.origin is None:
        raise RuntimeError("procrastinate is not installed")
    return Path(spec.origin).resolve().parent / "contrib" / "alembic" / "versions"


# Declared list of version directories (no scan): SRV-AUTH's, SRV-LESSON's, SRV-EVENTS's, SRV-SEARCH's,
# procrastinate's.
VERSION_LOCATIONS: Final[list[Path]] = [
    SCRIPT_LOCATION / "versions",
    _HERE.parent / "lessons" / "migrations",
    _HERE.parent / "jobs" / "migrations",
    _HERE.parent / "search" / "migrations",
    _procrastinate_versions(),
]
# pg_advisory_lock(namespace, key): this namespace is for migrations only ("RDMG"); SRV-EVENTS uses another.
MIGRATION_LOCK_NAMESPACE: Final = 0x52444D47
MIGRATION_LOCK_KEY: Final = 1
MIGRATION_LOCK_POLL_S: Final = 0.2  # structural: a waiter retries the try-lock; migrations take seconds
# structural: far above a full upgrade from empty (seconds in the tests); a holder stuck longer is a fault
MIGRATION_LOCK_WAIT_S: Final = 300.0


class MigrationError(RuntimeError):
    """A migration lock failure; `code` is fixed text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class DbTarget:
    host: str
    port: int
    user: str
    database: str
    password: Secret = field(repr=False)


def make_engine(target: DbTarget) -> AsyncEngine:
    url = URL.create(
        "postgresql+asyncpg",
        username=target.user,
        host=target.host,
        port=target.port,
        database=target.database,
    )
    password = target.password

    async def _password() -> str:
        return password.reveal()

    # asyncpg accepts a callable password; the URL stays password-free. hide_parameters: a SQL error never
    # logs bound parameters such as a search query or a token hash (paired SRV-SEARCH change, FR-AUTH-1).
    return create_async_engine(
        url, connect_args={"password": _password}, pool_pre_ping=True, hide_parameters=True
    )


def alembic_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(SCRIPT_LOCATION))
    config.set_main_option("version_locations", " ".join(str(p) for p in VERSION_LOCATIONS))
    config.set_main_option("path_separator", "space")
    return config


def _upgrade(connection: Connection) -> None:
    config = alembic_config()
    config.attributes["connection"] = connection
    command.upgrade(config, "heads")


def _heads_reached(connection: Connection) -> bool:
    script = ScriptDirectory.from_config(alembic_config())
    current = set(MigrationContext.configure(connection).get_current_heads())
    # Effective heads: a head reached only as another revision's `depends_on` (procrastinate_0043 under
    # jobs_0001) is not kept in the version table, so it is not compared.
    return current == {r.revision for r in script.get_revisions("heads") if r is not None}


async def migrate(engine: AsyncEngine) -> None:
    """`alembic upgrade heads` under the migration advisory lock.

    The session lock is held on its own autocommit connection and taken by polling `pg_try_advisory_lock`,
    so a waiting migrator never holds an open transaction: procrastinate's chain runs `CREATE INDEX
    CONCURRENTLY`, which waits for every open transaction and would deadlock against a waiter blocked in
    `pg_advisory_lock`.
    """
    lock = {"ns": MIGRATION_LOCK_NAMESPACE, "key": MIGRATION_LOCK_KEY}
    loop = asyncio.get_running_loop()
    give_up = loop.time() + MIGRATION_LOCK_WAIT_S
    async with engine.connect() as raw:
        holder = await raw.execution_options(isolation_level="AUTOCOMMIT")
        while True:  # the lock lives in Postgres, so it is polled, not awaited as an event
            if (await holder.execute(text("SELECT pg_try_advisory_lock(:ns, :key)"), lock)).scalar():
                break
            if loop.time() >= give_up:
                raise MigrationError("migration_lock_timeout")
            await asyncio.sleep(MIGRATION_LOCK_POLL_S)
        try:
            async with engine.connect() as conn:
                await conn.run_sync(_upgrade)
                await conn.commit()
        finally:
            released = (await holder.execute(text("SELECT pg_advisory_unlock(:ns, :key)"), lock)).scalar()
            if not released:
                raise MigrationError("migration_unlock_failed")


async def at_heads(engine: AsyncEngine) -> bool:
    async with engine.connect() as conn:
        return await conn.run_sync(_heads_reached)


async def wait_for_heads(
    engine: AsyncEngine,
    stop: asyncio.Event,
    interval_s: float = 1.0,
    on_error: Callable[[], None] | None = None,
) -> bool:
    """Poll until the schema is at all heads or `stop` is set; never migrates (the worker role).

    A failed poll calls `on_error` (no argument: the error may carry driver detail) and polls again.
    """
    while not stop.is_set():
        try:
            if await at_heads(engine):
                return True
        except Exception:  # any database failure: retry; the caller reports without detail
            if on_error is not None:
                on_error()
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except TimeoutError:
            continue
    return False
