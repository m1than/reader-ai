"""Fixtures and helpers for the SRV-SEARCH tests.

@trace FR-SEARCH-4

Modules under test are imported through `need()` inside test bodies or helpers, so a missing module is an
assertion failure (Red), not a collection error. Postgres runs from the pinned pgvector-enabled image
(SRV-SEARCH header).

SRV-INGEST's tables come from its migration `ingest_0001` (FR-SEARCH-4 integration note): `add_material`
inserts rows that satisfy that schema and creates no tables.

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
        self.active = 0  # calls inside encode() right now (INV-SEARCH-8)
        self.max_active = 0

    def encode(self, kind: str, texts: Sequence[str]) -> list[list[float]]:
        with self.lock:
            self.calls.append((kind, len(texts)))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay_s:
                time.sleep(self.delay_s)
            return [hash_vector(t) for t in texts]
        finally:
            with self.lock:
                self.active -= 1


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

@dataclass
class World:
    engine: Any
    settings: Any
    users: dict[str, uuid.UUID]
    config_path: Path


async def world(pg: Pg, tmp: Path, monkeypatch: pytest.MonkeyPatch | None = None) -> World:
    """A migrated database (all heads, search and ingest included), ivan (owner), anna (learner)."""
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
        users = {r[0]: r[1] for r in await conn.execute(text("SELECT handle, id FROM users"))}
    return World(engine, settings, users, path)


async def add_material(
    w: World,
    handle: str,
    sha: str,
    spans: Sequence[tuple[int, str]],
    config_id: str | None = "book-ru",
    kinds: Sequence[str] | None = None,
    outline: Sequence[tuple[int, str, int | None]] = (),
) -> uuid.UUID:
    """A material with one ingest, its spans ((page, text) in seq order) and its outline ((level, title, page)
    in pre-order), sealed as an active pack. Returns the ingest id."""
    from sqlalchemy import text

    owner = w.users[handle]
    async with w.engine.begin() as conn:
        material = (
            await conn.execute(
                text(
                    "INSERT INTO materials (owner_id, sha256, config_id, page_count) VALUES (:o, :s, :c, :n)"
                    " RETURNING id"
                ),
                {"o": owner, "s": sha, "c": config_id, "n": max([1, *(page for page, _ in spans)])},
            )
        ).scalar_one()
        ingest = (
            await conn.execute(
                text(
                    "INSERT INTO ingests (owner_id, material_id, converter_version, artifact_id, span_count,"
                    " outline_count, outline_source, pages_without_text)"
                    " VALUES (:o, :m, 'test-1', :a, :n, 0, 'headings', '{}') RETURNING id"
                ),
                {"o": owner, "m": material, "a": "0" * 64, "n": len(spans)},
            )
        ).scalar_one()
        span_kinds = list(kinds or ["text"] * len(spans))
        for seq, (page, body) in enumerate(spans):
            await conn.execute(
                text(
                    "INSERT INTO material_spans (ingest_id, owner_id, seq, page, kind, text, x0, y0, x1, y1)"
                    " VALUES (:i, :o, :q, :p, :k, :t, 0, 0, 0, 0)"
                ),
                {"i": ingest, "o": owner, "q": seq, "p": page, "k": span_kinds[seq], "t": body},
            )
        for seq, (level, title, page) in enumerate(outline):
            await conn.execute(
                text(
                    "INSERT INTO material_outline (ingest_id, owner_id, seq, level, title, page)"
                    " VALUES (:i, :o, :q, :l, :t, :p)"
                ),
                {"i": ingest, "o": owner, "q": seq, "l": level, "t": title, "p": page},
            )
        from ingest.packseed import seal  # FR-SEARCH-5: the ingest's pack, active (searched) by default

        pid = await seal(conn, owner, ingest, sha)
        language = LANGUAGES.get(config_id or "")
        if language is not None:  # FR-SEARCH-6: the language reaches search through the profile overlay
            await _head_profile(conn, owner, pid, {"language": language})
    result: uuid.UUID = ingest
    return result


LANGUAGES = {"book-ru": "ru", "book-en": "en"}  # reader.yaml's languages in CONFIG


async def _head_profile(conn: Any, owner: uuid.UUID, pid: str, values: dict[str, Any]) -> None:
    from reader.ingest import manifest, overlays

    oid = await overlays.build_profile(conn, owner, pid, values)
    seq, top = await manifest.head(conn, owner)
    elements = {e["pack_id"]: dict(e["overlays"]) for e in top["packs"]}
    elements[pid]["profile"] = oid
    await manifest.append(conn, owner, seq, manifest.make_entry(elements), "test_fixture")


async def set_profile(w: World, handle: str, sha: str, license_: str, language: str = "en") -> None:
    """Give the material's active pack a profile overlay in a new head entry (SRV-INGEST FR-INGEST-36)."""
    from reader.ingest import manifest, overlays, views

    owner = w.users[handle]
    async with w.engine.begin() as conn:
        pack = await views.active_pack(conn, owner, sha)
        assert pack is not None
        values = {"license": license_, "language": language}
        oid = await overlays.build_profile(conn, owner, pack.pack_id, values)
        seq, top = await manifest.head(conn, owner)
        elements = {e["pack_id"]: dict(e["overlays"]) for e in top["packs"]}
        elements[pack.pack_id]["profile"] = oid
        await manifest.append(conn, owner, seq, manifest.make_entry(elements), "test_fixture")


async def scalar(engine: Any, sql: str, **params: Any) -> Any:
    from sqlalchemy import text

    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar()


async def embed_args(w: World, owner: uuid.UUID, sha: str) -> Any:
    """The material's current `search.embed` args (its buildable pack's current vector overlay)."""
    jobs = need("reader.search.jobs")
    async with w.engine.connect() as conn:
        args = await jobs.current_args(conn, owner, sha)
    assert args is not None
    return args


async def run_embed(w: World, args: Any) -> None:
    """Run the `search.embed` handler inside one transaction, as the worker does."""
    jobs = need("reader.search.jobs")
    from reader.jobs.model import JobContext

    async with w.engine.begin() as conn:
        await jobs.handle_embed(args, conn, JobContext(job_id=1, key="test", attempts=0))


async def embed_material(w: World, ingest: uuid.UUID, sha: str) -> None:
    """Embed the material of an ingest for that ingest's owner (its buildable pack's current overlay)."""
    owner = await scalar(w.engine, "SELECT owner_id FROM ingests WHERE id = :i", i=ingest)
    await run_embed(w, await embed_args(w, owner, sha))


async def reset(w: World) -> None:
    """Empty the search and test-only ingest tables (between property-test examples). Packs and the manifest
    are tombstones and append-only under SRV-INGEST's triggers, so this session switches triggers off."""
    from sqlalchemy import text

    async with w.engine.begin() as conn:
        await conn.execute(text("SET LOCAL session_replication_role = replica"))
        for table in ("search_span_vectors", "pack_profiles", "overlays", "manifest_entries",
                      "pending_activations", "material_spans", "base_packs", "ingests", "materials"):
            await conn.execute(text(f"DELETE FROM {table}"))
