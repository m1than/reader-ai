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

# The image OPS-DEPLOY pins (deploy/images/postgres.Dockerfile): postgres:18-trixie.
PG_IMAGE = "postgres:18-trixie@sha256:eb63372c1e05c755f55fa30cbd07aecc443676fc6c0fa90b9002d01537841e1a"


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
