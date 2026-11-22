"""Database engine and migrations (SRV-AUTH FR-AUTH-1; ADR-0006).

The password reaches asyncpg as a separate connect argument, so no URL or DSN string ever contains it.
Migrations run through the app's asyncpg engine (`run_sync`) under a two-key advisory lock in this module's
namespace, to all heads of the history whose directories `VERSION_LOCATIONS` declares (Alembic does not glob).
"""

from __future__ import annotations

import asyncio
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
# Declared list of version directories (no scan). SRV-LESSON adds its own; SRV-EVENTS appends its own and
# procrastinate's.
VERSION_LOCATIONS: Final[list[Path]] = [SCRIPT_LOCATION / "versions", _HERE.parent / "lessons" / "migrations"]
# pg_advisory_lock(namespace, key): this namespace is for migrations only ("RDMG"); SRV-EVENTS uses another.
MIGRATION_LOCK_NAMESPACE: Final = 0x52444D47
MIGRATION_LOCK_KEY: Final = 1


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

    # asyncpg accepts a callable password; the URL stays password-free.
    return create_async_engine(url, connect_args={"password": _password}, pool_pre_ping=True)


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
    return current == set(script.get_heads())


async def migrate(engine: AsyncEngine) -> None:
    """`alembic upgrade heads` under the migration advisory lock."""
    async with engine.connect() as conn:
        await conn.execute(
            text("SELECT pg_advisory_lock(:ns, :key)"),
            {"ns": MIGRATION_LOCK_NAMESPACE, "key": MIGRATION_LOCK_KEY},
        )
        try:
            await conn.commit()
            await conn.run_sync(_upgrade)
            await conn.commit()
        finally:
            await conn.execute(
                text("SELECT pg_advisory_unlock(:ns, :key)"),
                {"ns": MIGRATION_LOCK_NAMESPACE, "key": MIGRATION_LOCK_KEY},
            )
            await conn.commit()


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
