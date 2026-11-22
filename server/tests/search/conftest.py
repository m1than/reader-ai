"""Fixtures and helpers for the SRV-SEARCH tests.

@trace FR-SEARCH-4

Modules under test are imported through `need()` inside test bodies or helpers, so a missing module is an
assertion failure (Red), not a collection error. Postgres runs from the pinned pgvector-enabled image
(SRV-SEARCH header).

SRV-INGEST's tables are not on the integration branch yet (declared deviation, FR-SEARCH-4): `INGEST_TABLES`
creates the three tables search reads, with SRV-INGEST FR-INGEST-14's columns, as a test-only helper.

The fake embed service is the real `reader.search.embed_service` HTTP server with a deterministic
`HashEncoder`: a hashed bag of words, so texts sharing words are closer, and every vector is unit length.
"""

from __future__ import annotations

import hashlib
import importlib
import math
import re
import secrets
import shutil
import subprocess
import textwrap
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from ruamel.yaml import YAML

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "spec" / "fixtures" / "SRV-SEARCH"
ORIGIN = "https://localhost"
SESSION_COOKIE = "__Host-reader_session"
PG_IMAGE = (
    "pgvector/pgvector:0.8.7-pg18-trixie@sha256:ab49524d18df7200b74ac9a475e1db6297fc1eb57da4c214c63d10ecc4b754fa"
)
DIM = 1024
WORD = re.compile(r"\w+", re.UNICODE)


def need(name: str) -> ModuleType:
    """Import a module under test; a missing module is an assertion failure, not a collection error."""
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as error:
        raise AssertionError(f"{name} is not implemented ({error.name} missing)") from None


def fixture(name: str) -> Any:
    return YAML(typ="safe").load((FIXTURES / name).read_text(encoding="utf-8"))


# ---- the fake encoder -----------------------------------------------------------------------------------


def hash_vector(text: str) -> list[float]:
    """Deterministic unit vector: each lower-cased word adds 1 at a hashed index."""
    vec = [0.0] * DIM
    words = WORD.findall(text.lower()) or [text]
    for word in words:
        index = int.from_bytes(hashlib.sha256(word.encode("utf-8")).digest()[:4], "big") % DIM
        vec[index] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class HashEncoder:
    """The `Encoder` protocol of embed_service, deterministic; `delay_s` simulates a slow batch."""

    def __init__(self, delay_s: float = 0.0, model: str | None = None) -> None:
        self.delay_s = delay_s
        self.model_override = model
        self.calls: list[tuple[str, int]] = []
        self.lock = threading.Lock()

    def encode(self, kind: str, texts: Sequence[str]) -> list[list[float]]:
        with self.lock:
            self.calls.append((kind, len(texts)))
        if self.delay_s:
            time.sleep(self.delay_s)
        return [hash_vector(t) for t in texts]


@dataclass
class EmbedServer:
    url: str
    encoder: HashEncoder
    server: Any


@pytest.fixture
def embed_server() -> Iterator[EmbedServer]:
    svc = need("reader.search.embed_service")
    params = need("reader.search.params")
    encoder = HashEncoder()
    server = svc.make_server(encoder, "127.0.0.1", 0, model_id=params.MODEL_ID)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield EmbedServer(f"http://{host}:{port}", encoder, server)
    finally:
        server.shutdown()
        server.server_close()


# ---- Postgres -------------------------------------------------------------------------------------------


def _daemon_up() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0


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
materials:
  - { id: book-ru, file: ./ru.pdf, sha256: "%s", license: test, language: ru }
  - { id: book-en, file: ./en.pdf, sha256: "%s", license: test, language: en }
sharing: {}
"""

MAT_RU = "a" * 64
MAT_EN = "b" * 64

# Test-only: SRV-INGEST FR-INGEST-14's tables, the columns search reads (declared deviation, FR-SEARCH-4).
INGEST_TABLES = (
    """CREATE TABLE IF NOT EXISTS materials (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users (id),
        sha256 text NOT NULL, config_id text, page_count int, created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (owner_id, sha256))""",
    """CREATE TABLE IF NOT EXISTS ingests (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES users (id),
        material_id uuid NOT NULL UNIQUE REFERENCES materials (id), converter_version text NOT NULL,
        artifact_id text, span_count int, outline_count int, outline_source text, pages_without_text int[],
        imported_at timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS material_spans (
        ingest_id uuid NOT NULL REFERENCES ingests (id), owner_id uuid NOT NULL REFERENCES users (id),
        seq int NOT NULL, page int NOT NULL, kind text NOT NULL, level int, decoded bool, text text NOT NULL,
        x0 real, y0 real, x1 real, y1 real, PRIMARY KEY (ingest_id, seq))""",
)


@dataclass
class World:
    engine: Any
    settings: Any
    users: dict[str, uuid.UUID]
    config_path: Path


async def world(pg: Pg, tmp: Path, monkeypatch: pytest.MonkeyPatch | None = None) -> World:
    """A migrated database (all heads, search included), ivan (owner), anna (learner), the ingest tables."""
    from sqlalchemy import text

    from reader.auth.policy import settings_from_config
    from reader.auth.store import prepare
    from reader.config import Secret, load_config
    from reader.db import DbTarget, make_engine, migrate

    path = tmp / "reader.yaml"
    path.write_text(textwrap.dedent(CONFIG % (MAT_RU, MAT_EN)), encoding="utf-8")
    if monkeypatch is not None:
        monkeypatch.setenv("READER_CONFIG", str(path))
    settings = settings_from_config(load_config(path))
    engine = make_engine(DbTarget(pg.host, pg.port, pg.user, pg.database, Secret(pg.password)))
    await migrate(engine)
    await prepare(engine, settings)
    async with engine.begin() as conn:
        for statement in INGEST_TABLES:
            await conn.execute(text(statement))
        users = {r[0]: r[1] for r in await conn.execute(text("SELECT handle, id FROM users"))}
    return World(engine, settings, users, path)


async def add_material(
    w: World,
    handle: str,
    sha: str,
    spans: Sequence[tuple[int, str]],
    config_id: str | None = "book-ru",
    kinds: Sequence[str] | None = None,
) -> uuid.UUID:
    """A material with one ingest and its spans: (page, text) in seq order. Returns the ingest id."""
    from sqlalchemy import text

    owner = w.users[handle]
    async with w.engine.begin() as conn:
        material = (
            await conn.execute(
                text("INSERT INTO materials (owner_id, sha256, config_id) VALUES (:o, :s, :c) RETURNING id"),
                {"o": owner, "s": sha, "c": config_id},
            )
        ).scalar_one()
        ingest = (
            await conn.execute(
                text(
                    "INSERT INTO ingests (owner_id, material_id, converter_version) VALUES (:o, :m, 'test-1')"
                    " RETURNING id"
                ),
                {"o": owner, "m": material},
            )
        ).scalar_one()
        span_kinds = list(kinds or ["text"] * len(spans))
        for seq, (page, body) in enumerate(spans):
            await conn.execute(
                text(
                    "INSERT INTO material_spans (ingest_id, owner_id, seq, page, kind, text)"
                    " VALUES (:i, :o, :q, :p, :k, :t)"
                ),
                {"i": ingest, "o": owner, "q": seq, "p": page, "k": span_kinds[seq], "t": body},
            )
    result: uuid.UUID = ingest
    return result


async def scalar(engine: Any, sql: str, **params: Any) -> Any:
    from sqlalchemy import text

    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar()


async def embed_material(w: World, ingest: uuid.UUID, sha: str) -> None:
    """Run the `search.embed` handler for one ingest inside one transaction, as the worker does."""
    jobs = need("reader.search.jobs")
    from reader.jobs.model import JobContext

    args = jobs.embed_args(sha, ingest, fts_config=await current_fts(w, sha))
    async with w.engine.begin() as conn:
        await jobs.handle_embed(args, conn, JobContext(job_id=1, key="test", attempts=0))


async def current_fts(w: World, sha: str) -> str:
    params = need("reader.search.params")
    config_id = await scalar(w.engine, "SELECT config_id FROM materials WHERE sha256 = :s", s=sha)
    language = {"book-ru": "ru", "book-en": "en"}.get(config_id or "")
    value: str = params.fts_config_for(language)
    return value
