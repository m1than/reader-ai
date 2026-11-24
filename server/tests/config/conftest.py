"""Shared helpers for the SRV-CONFIG tests: fixture loading and small utilities.

The module under test (`reader.config`) is imported inside each test body, never here, so that in the Red
phase every test fails on its own (spec/modules/SRV-CONFIG.yaml, header note).
"""

import os
import secrets
import shutil
import subprocess
import traceback
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML
from settings_helpers import Pg

FIXTURES = Path(__file__).resolve().parents[3] / "spec" / "fixtures" / "SRV-CONFIG"
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
skip_as_root = pytest.mark.skipif(IS_ROOT, reason="permissions do not apply to uid 0")


def load_fixture(name: str) -> Any:
    """Read a spec fixture with a plain YAML 1.2 safe loader (the fixtures are trusted test data)."""
    return YAML(typ="safe", pure=True).load((FIXTURES / name).read_text(encoding="utf-8"))


def write_config(directory: Path, text: str, name: str = "reader.yaml") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def rendered(error: BaseException) -> str:
    """Every textual surface of an exception that a log or a crash report could show."""
    return "\n".join([
        str(error),
        repr(error),
        "".join(traceback.format_exception(error)),
    ])


def rendered_with_locals(error: BaseException) -> str:
    return "".join(traceback.TracebackException.from_exception(error, capture_locals=True).format())


def canaries(text: str) -> list[str]:
    """The CANARY-… tokens planted in a fixture text."""
    return sorted({word.strip("\"',{}:[]") for word in text.split() if "CANARY-" in word})


# Postgres for the settings overlay's [db] tests (FR-CONFIG-25/-26): the pinned pgvector image of the other
# server tests, one fresh database per test; skipped when no Docker daemon is reachable.
PG_IMAGE = (
    "pgvector/pgvector:0.8.7-pg18-trixie@sha256:ab49524d18df7200b74ac9a475e1db6297fc1eb57da4c214c63d10ecc4b754fa"
)


def _daemon_up() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False, timeout=20).returncode == 0  # noqa: S607


@pytest.fixture(scope="session")
def pg_container() -> Iterator[tuple[Pg, Any]]:
    if not _daemon_up():
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
    server, container = pg_container
    name = "t_" + secrets.token_hex(6)
    code, out = container.exec(["psql", "-U", "postgres", "-c", f"CREATE DATABASE {name}"])
    assert code == 0, out
    return Pg(server.host, server.port, server.user, server.password, name)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
