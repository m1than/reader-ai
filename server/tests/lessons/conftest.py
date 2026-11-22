"""Fixtures and helpers for the SRV-LESSON tests.

@trace FR-LESSON-14

The modules under test (`reader.lessons`, `reader.blobs`) are imported only through `need()` inside test
bodies or helpers they call, which turns a missing module into an assertion failure (Red phase). Tests that
need Postgres use the pinned image through testcontainers and skip cleanly when no Docker daemon is reachable.
"""

from __future__ import annotations

import copy
import importlib
import json
import secrets
import shutil
import subprocess
import textwrap
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from ruamel.yaml import YAML

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "spec" / "fixtures" / "SRV-LESSON"
SPEC = REPO / "spec" / "modules" / "SRV-LESSON.yaml"
PACKAGE = REPO / "server" / "src" / "reader" / "lessons"
F01 = (FIXTURES / "F01.ipynb").read_bytes()
ORIGIN = "https://localhost"
SESSION_COOKIE = "__Host-reader_session"
# The image OPS-DEPLOY pins (deploy/images/postgres.Dockerfile).
PG_IMAGE = "postgres:18-trixie@sha256:eb63372c1e05c755f55fa30cbd07aecc443676fc6c0fa90b9002d01537841e1a"


def spec_data() -> dict[str, Any]:
    data: dict[str, Any] = YAML(typ="safe").load(SPEC.read_text(encoding="utf-8"))
    return data


def cases() -> dict[str, Any]:
    data: dict[str, Any] = YAML(typ="safe").load((FIXTURES / "invalid.yaml").read_text(encoding="utf-8"))
    return data


def need(name: str) -> ModuleType:
    """Import a module under test; a missing module is an assertion failure, not a collection error."""
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as error:
        raise AssertionError(f"{name} is not implemented ({error.name} missing)") from None


# --- VEC-LESSON-3 mutations (RFC 6902 subset: add, remove, replace, move) ---


def _parts(pointer: str) -> list[str]:
    return [p.replace("~1", "/").replace("~0", "~") for p in pointer.split("/")[1:]]


def _parent(doc: Any, pointer: str) -> tuple[Any, str]:
    *head, last = _parts(pointer)
    node = doc
    for part in head:
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node, last


def _remove(doc: Any, pointer: str) -> Any:
    node, last = _parent(doc, pointer)
    return node.pop(int(last)) if isinstance(node, list) else node.pop(last)


def _add(doc: Any, pointer: str, value: Any) -> None:
    node, last = _parent(doc, pointer)
    if isinstance(node, list):
        node.insert(len(node) if last == "-" else int(last), value)
    else:
        node[last] = value


def apply_ops(doc: Any, ops: list[dict[str, Any]]) -> Any:
    doc = copy.deepcopy(doc)
    for op in ops:
        kind = op["op"]
        if kind == "remove":
            _remove(doc, op["path"])
        elif kind == "add":
            _add(doc, op["path"], copy.deepcopy(op["value"]))
        elif kind == "replace":
            node, last = _parent(doc, op["path"])
            if isinstance(node, list):
                node[int(last)] = copy.deepcopy(op["value"])
            else:
                node[last] = copy.deepcopy(op["value"])
        elif kind == "move":
            _add(doc, op["path"], _remove(doc, op["from"]))
        else:
            raise ValueError(kind)
    return doc


def case_bytes(case: dict[str, Any], base: bytes = F01) -> bytes:
    """The input bytes of one VEC-LESSON-3 case."""
    if "raw" in case:
        return str(case["raw"]).encode("utf-8")
    if "raw_hex" in case:
        return bytes.fromhex(str(case["raw_hex"]).replace(" ", ""))
    if "raw_len" in case:
        return b" " * int(case["raw_len"])
    doc = apply_ops(json.loads(base), case.get("ops") or [])
    if "pad_to_bytes" in case:
        target = int(case["pad_to_bytes"])
        notes = next(c for c in doc["cells"] if c["id"] == "f01-notes")
        notes["source"] = ["x"]
        size = len(json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False).encode())
        notes["source"] = ["x" * (target - size + 64)]  # margin over small serialiser differences
    text = json.dumps(doc, ensure_ascii=False)
    for old, new in (case.get("replace_text") or {}).items():
        text = text.replace(old, new)
    return text.encode("utf-8", "surrogatepass")


# --- Postgres ---


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


CONFIG = """\
version: 1
instance: { url: "https://localhost", data_dir: ./data }
users:
  - { handle: ivan, role: owner }
  - { handle: anna, role: learner }
sharing: {}
"""


@dataclass
class World:
    engine: Any
    settings: Any
    blobs: Path
    users: dict[str, uuid.UUID]


async def world(pg: Pg, tmp: Path) -> World:
    """A migrated database with users ivan (owner) and anna (learner), and an empty blob root."""
    from sqlalchemy import text

    from reader.auth.policy import settings_from_config
    from reader.auth.store import prepare
    from reader.config import Secret, load_config
    from reader.db import DbTarget, make_engine, migrate

    path = tmp / "reader.yaml"
    path.write_text(textwrap.dedent(CONFIG), encoding="utf-8")
    settings = settings_from_config(load_config(path))
    engine = make_engine(DbTarget(pg.host, pg.port, pg.user, pg.database, Secret(pg.password)))
    await migrate(engine)
    await prepare(engine, settings)
    async with engine.connect() as conn:
        users = {r[0]: r[1] for r in await conn.execute(text("SELECT handle, id FROM users"))}
    blobs = tmp / "blobs"
    blobs.mkdir()
    return World(engine, settings, blobs, users)


async def session_for(w: World, handle: str) -> str:
    """A session cookie value through SRV-AUTH's own session function (no WebAuthn ceremony)."""
    from reader.auth.store import _start_session

    async with w.engine.begin() as conn:
        return await _start_session(conn, w.users[handle], None)


async def course_and_lesson(w: World, handle: str = "ivan", ordinal: int = 1) -> tuple[uuid.UUID, uuid.UUID]:
    store = need("reader.lessons.store")
    async with w.engine.begin() as conn:
        course = await store.create_course(conn, w.users[handle], f"Course of {handle}", None)
        lesson = await store.ensure_lesson(conn, w.users[handle], course, ordinal, "Lesson")
    return course, lesson


async def write(
    w: World,
    handle: str,
    lesson: uuid.UUID,
    data: bytes = F01,
    stage: str = "draft",
    provenance: dict[str, Any] | None = None,
) -> tuple[str, bool]:
    store = need("reader.lessons.store")
    async with w.engine.begin() as conn:
        result: tuple[str, bool] = await store.write_revision(
            conn, w.blobs, w.users[handle], lesson, data, stage, provenance or {}
        )
        return result


async def refusal(awaitable: Any) -> str:
    """The error code of a write that must be refused (LessonWriteError.code)."""
    try:
        await awaitable
    except Exception as error:
        store = need("reader.lessons.store")
        if not isinstance(error, store.LessonWriteError):
            raise
        return str(error.code)
    raise AssertionError("the write was accepted")


async def scalar(engine: Any, sql: str, **params: Any) -> Any:
    from sqlalchemy import text

    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar()


@asynccontextmanager
async def client(w: World, handle: str | None = "ivan") -> AsyncIterator[httpx.AsyncClient]:
    from reader.api.app import create_app

    app = create_app(w.settings, w.engine, run_cleanup_task=False)
    app.state.blobs_root = w.blobs
    cookies = {SESSION_COOKIE: await session_for(w, handle)} if handle else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url=ORIGIN, headers={"Origin": ORIGIN}, cookies=cookies
    ) as c:
        yield c
