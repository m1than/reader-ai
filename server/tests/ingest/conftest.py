"""Fixtures and helpers for the SRV-INGEST tests (spec/modules/SRV-INGEST.yaml).

@trace VEC-INGEST-1

Modules under test are imported only through `need()` inside test bodies, so in the Red phase every test fails
on an assertion in its own body. [db] tests use the pinned Postgres image through testcontainers and skip
without a Docker daemon. [host] tests need the converter venv from `reader-ingest --setup` and run only with
READER_TEST_INGEST_HOST=1; [local] tests also need the pilot PDF (READER_TEST_PILOT_PDF) and report a skip,
never a pass, when it is absent.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import secrets
import shutil
import subprocess
import textwrap
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from . import make_artifact_fixture as fixture_gen

REPO = Path(__file__).resolve().parents[3]
SPEC = REPO / "spec" / "modules" / "SRV-INGEST.yaml"
FIXTURES = REPO / "spec" / "fixtures" / "SRV-INGEST"
ARTIFACT_OK = FIXTURES / "artifact-ok"
ARTIFACT_OK_ID = (FIXTURES / "artifact-ok.id").read_text().strip()
SRC = REPO / "server" / "src"
ORIGIN = "https://localhost"
SESSION_COOKIE = "__Host-reader_session"
# postgres:18-trixie plus pgvector 0.8.7: the heads include SRV-SEARCH's migration (CREATE EXTENSION vector).
PG_IMAGE = (
    "pgvector/pgvector:0.8.7-pg18-trixie@sha256:ab49524d18df7200b74ac9a475e1db6297fc1eb57da4c214c63d10ecc4b754fa"
)
MATERIAL_SHA = "a" * 64
PILOT_SHA = "61e8d4c390ba63c3f13efe0860d5042f8d1b19ba8f26b4f2e14e73478bc9e2b2"
CANARY = "QUOKKAZEBRA"

HOST = os.environ.get("READER_TEST_INGEST_HOST") == "1"
host_only = pytest.mark.ingest_host


def need(name: str) -> ModuleType:
    """Import a module under test; a missing module is an assertion failure, not a collection error."""
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as error:
        raise AssertionError(f"{name} is not implemented ({error.name} missing)") from None


def artifact_files() -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(ARTIFACT_OK.iterdir())}


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def build_artifact(
    spans: list[dict[str, Any]] | None = None,
    outline: list[dict[str, Any]] | None = None,
    keep_locators: bool = False,
    **manifest: Any,
) -> dict[str, bytes]:
    """The three artifact files, derived from VEC-INGEST-1 with optional replacements (hashes recomputed).

    Locators, quote hashes, parse_config and pack_id (format 2) are recomputed by the independent generator
    for the material and version in effect, unless `keep_locators` (spans as given) or the caller names
    them."""
    base = artifact_files()
    span_rows = spans if spans is not None else [json.loads(x) for x in base["spans.jsonl"].splitlines()]
    meta = json.loads(base["manifest.json"])
    sha = str(manifest.get("material_sha256", meta["material_sha256"]))
    version = str(manifest.get("converter_version", meta["converter_version"]))
    if not keep_locators:
        bare = [{k: v for k, v in s.items() if k not in ("locator", "quote_sha256")} for s in span_rows]
        try:
            span_rows = fixture_gen.with_locators(bare, sha)
        except (KeyError, TypeError, ValueError):  # a span broken on purpose (no page, say): leave it bare
            span_rows = bare
    if version in fixture_gen.PARSE_CONFIGS:
        meta.update(fixture_gen.pack_fields(sha, version))
    outline_rows = outline if outline is not None else json.loads(base["outline.json"])
    spans_bytes = b"".join(canonical(s) + b"\n" for s in span_rows)
    outline_bytes = canonical(outline_rows)
    meta.update(
        span_count=len(span_rows),
        outline_count=len(outline_rows),
        spans_sha256=hashlib.sha256(spans_bytes).hexdigest(),
        outline_sha256=hashlib.sha256(outline_bytes).hexdigest(),
    )
    meta.update(manifest)
    return {"manifest.json": canonical(meta), "spans.jsonl": spans_bytes, "outline.json": outline_bytes}


def write_dir(root: Path, files: dict[str, bytes]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (root / name).write_bytes(body)
    return root


# --- a raw ustar writer for hostile tars (independent of reader.ingest's writer) ---


def tar_header(
    name: str, size: int, typeflag: bytes = b"0", magic: bytes = b"ustar\x0000", linkname: str = ""
) -> bytes:
    header = bytearray(512)
    header[0:100] = name.encode("utf-8")[:100].ljust(100, b"\0")
    header[100:108] = b"0000644\0"
    header[108:116] = b"0000000\0"
    header[116:124] = b"0000000\0"
    header[124:136] = f"{size:011o}\0".encode()
    header[136:148] = b"00000000000\0"
    header[148:156] = b" " * 8
    header[156:157] = typeflag
    header[157:257] = linkname.encode()[:100].ljust(100, b"\0")
    header[257:265] = magic
    checksum = sum(header)
    header[148:156] = f"{checksum:06o}\0 ".encode()
    return bytes(header)


def tar_member(name: str, body: bytes, typeflag: bytes = b"0", declared: int | None = None) -> bytes:
    size = len(body) if declared is None else declared
    pad = (-len(body)) % 512
    return tar_header(name, size, typeflag) + body + b"\0" * pad


def tar_of(files: dict[str, bytes], order: tuple[str, ...] | None = None, end: bytes = b"\0" * 1024) -> bytes:
    names = order or ("manifest.json", "spans.jsonl", "outline.json")
    return b"".join(tar_member(n, files[n]) for n in names) + end


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
    server, container = pg_container
    name = "i_" + secrets.token_hex(6)
    code, out = container.exec(["psql", "-U", "postgres", "-c", f"CREATE DATABASE {name}"])
    assert code == 0, out
    return Pg(server.host, server.port, server.user, server.password, name)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


CONFIG = """\
version: 1
instance: {{ url: "https://localhost", data_dir: ./data }}
users:
  - {{ handle: ivan, role: owner }}
  - {{ handle: anna, role: learner }}
materials:
  - {{ id: synthetic, file: ./m.pdf, sha256: "{sha}", license: personal-use, language: ru }}
  - {{ id: other, file: ./o.pdf, sha256: "{other}", license: shareable, language: en }}
sharing: {{}}
"""


@dataclass
class World:
    engine: Any
    settings: Any
    config: Any
    blobs: Path
    users: dict[str, uuid.UUID]
    pg: Pg
    app: Any = None
    jobs: Any = None
    logs: list[str] = field(default_factory=list)

    async def scalar(self, sql: str, **params: Any) -> Any:
        from sqlalchemy import text

        async with self.engine.connect() as conn:
            return (await conn.execute(text(sql), params)).scalar()

    async def rows(self, sql: str, **params: Any) -> list[Any]:
        from sqlalchemy import text

        async with self.engine.connect() as conn:
            return list((await conn.execute(text(sql), params)).all())

    async def work(self) -> None:
        """Run the worker until the queues are empty."""
        await self.app.run_worker_async(
            queues=["ingest"], wait=False, install_signal_handlers=False, listen_notify=False
        )


COURSE_COLUMNS = (
    "ALTER TABLE courses ADD COLUMN IF NOT EXISTS deleted_at timestamptz NULL",
    "ALTER TABLE courses ADD COLUMN IF NOT EXISTS deleted_by_material text NULL",
    "ALTER TABLE courses ADD COLUMN IF NOT EXISTS archived_at timestamptz NULL",
    "ALTER TABLE courses ADD COLUMN IF NOT EXISTS position integer NULL",
    "ALTER TABLE lessons ADD COLUMN IF NOT EXISTS deleted_at timestamptz NULL",
)


async def course_columns(engine: Any) -> None:
    """Pre-merge shim (FR-INGEST-31): SRV-LESSON's manage columns that the material routes read, until the
    lessons manage migration (V/manage-impl-lesson) is in the heads; IF NOT EXISTS makes it a no-op after."""
    from sqlalchemy import text

    async with engine.begin() as conn:
        for sql in COURSE_COLUMNS:
            await conn.execute(text(sql))


@asynccontextmanager
async def world(pg: Pg, tmp: Path) -> AsyncIterator[World]:
    """A migrated database (ingest tables included), users ivan (owner) and anna (learner), a blob root, the
    job runtime with the ingest kinds, and reader.yaml naming two materials."""
    from sqlalchemy import text

    from reader.auth.policy import settings_from_config
    from reader.auth.store import prepare
    from reader.config import Secret, load_config
    from reader.db import DbTarget, make_engine, migrate
    from reader.jobs.catalog import Catalog
    from reader.jobs.queue import Jobs
    from reader.jobs.worker import make_app

    ingest_jobs = need("reader.ingest.jobs")
    path = tmp / "reader.yaml"
    path.write_text(textwrap.dedent(CONFIG.format(sha=MATERIAL_SHA, other="b" * 64)), encoding="utf-8")
    config = load_config(path)
    settings = settings_from_config(config)
    target = DbTarget(pg.host, pg.port, pg.user, pg.database, Secret(pg.password))
    engine = make_engine(target)
    await migrate(engine)
    await prepare(engine, settings)
    async with engine.connect() as conn:
        users = {r[0]: r[1] for r in await conn.execute(text("SELECT handle, id FROM users"))}
    await course_columns(engine)
    blobs = tmp / "blobs"
    blobs.mkdir()
    app = make_app(target)
    await app.open_async()
    # The kinds of reader.ingest.jobs, built for this test's blob root (JOB_KINDS uses
    # /var/lib/reader/blobs), plus search.embed, which the import handler enqueues (FR-INGEST-13)
    # through reader.jobs.running(). work() runs only the ingest queue: the embed job is enqueued, never
    # executed here. The handler reads the material's language from READER_CONFIG, as in the worker.
    from reader.jobs import worker as jobs_worker
    from reader.search.jobs import JOB_KINDS as SEARCH_KINDS

    jobs = Jobs(engine, app, Catalog((*ingest_jobs.make_kinds(blobs), *SEARCH_KINDS)), [pg.password])
    w = World(engine, settings, config, blobs, users, pg, app, jobs)
    previous = jobs_worker._RUNNING[0]
    previous_config = os.environ.get("READER_CONFIG")
    jobs_worker._RUNNING[0] = jobs
    os.environ["READER_CONFIG"] = str(path)
    try:
        yield w
    finally:
        jobs_worker._RUNNING[0] = previous
        if previous_config is None:
            os.environ.pop("READER_CONFIG", None)
        else:
            os.environ["READER_CONFIG"] = previous_config
        await app.close_async()
        await engine.dispose()


async def session_for(w: World, handle: str) -> str:
    from reader.auth.store import _start_session

    async with w.engine.begin() as conn:
        return await _start_session(conn, w.users[handle], None)


@asynccontextmanager
async def client(
    w: World, handle: str | None = "ivan", *, jobs: Any = None
) -> AsyncIterator[httpx.AsyncClient]:
    from reader.api.app import create_app

    app = create_app(w.settings, w.engine, run_cleanup_task=False)
    app.state.blobs_root = w.blobs
    app.state.ingest_materials = w.config.materials
    if jobs is not None:  # the api's job runtime (routes that enqueue)
        app.state.jobs = jobs
    cookies = {SESSION_COOKIE: await session_for(w, handle)} if handle else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url=ORIGIN, headers={"Origin": ORIGIN}, cookies=cookies
    ) as c:
        yield c


def pilot_pdf() -> Path | None:
    raw = os.environ.get("READER_TEST_PILOT_PDF")
    if not raw:
        return None
    path = Path(raw)
    if not path.is_file():
        return None
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return path if digest == PILOT_SHA else None
