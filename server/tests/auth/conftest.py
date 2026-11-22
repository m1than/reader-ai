"""Fixtures for the SRV-AUTH tests: a Postgres server from the pinned image, one fresh database per test.

@trace FR-AUTH-1

The modules under test (`reader.auth`, `reader.db`, `reader.api`, `reader.serve`) are imported inside test
bodies or in helpers that test bodies call (auth_helpers.py), never here, so that in the Red phase every test
fails on its own.
Tests that need Postgres skip cleanly when no Docker daemon is reachable.
"""

from __future__ import annotations

import secrets
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

# postgres:18-trixie plus pgvector 0.8.7, the extension reader-postgres installs (OPS-DEPLOY FR-DEPLOY-2): the
# heads include SRV-SEARCH's migration, which creates the `vector` extension (SRV-SEARCH header).
PG_IMAGE = (
    "pgvector/pgvector:0.8.7-pg18-trixie@sha256:ab49524d18df7200b74ac9a475e1db6297fc1eb57da4c214c63d10ecc4b754fa"
)


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
        yield (
            Pg(c.get_container_host_ip(), int(c.get_exposed_port(5432)), "postgres", password, "postgres"),
            c,
        )


@pytest.fixture
def pg(pg_container: tuple[Pg, Any]) -> Pg:
    """A fresh, empty database on the shared server."""
    server, container = pg_container
    name = "t_" + secrets.token_hex(6)
    code, out = container.exec(["psql", "-U", "postgres", "-c", f"CREATE DATABASE {name}"])
    assert code == 0, out
    return Pg(server.host, server.port, server.user, server.password, name)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
