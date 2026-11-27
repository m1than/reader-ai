"""Shared fixtures for SRV-TUTOR tests: the spec and its vectors, a migrated database with a lesson, a material and
its spans, a fake find, a fake Provider port, and an api client with a session.

The module under test (`reader.tutor`) is imported only through `need()` inside tests, so a missing module fails
each test (Red) instead of the collection.
"""

from __future__ import annotations

import contextlib
import importlib
import secrets
import shutil
import subprocess
import textwrap
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from ruamel.yaml import YAML

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "spec" / "fixtures" / "SRV-TUTOR"
SPEC = REPO / "spec" / "modules" / "SRV-TUTOR.yaml"
F01 = (REPO / "server" / "src" / "reader" / "lessons" / "fixtures" / "F01.ipynb").read_bytes()
ORIGIN = "https://localhost"
SESSION_COOKIE = "__Host-reader_session"
# postgres:18-trixie plus pgvector 0.8.7: the heads include SRV-SEARCH's migration (CREATE EXTENSION vector).
PG_IMAGE = (
    "pgvector/pgvector:0.8.7-pg18-trixie@sha256:ab49524d18df7200b74ac9a475e1db6297fc1eb57da4c214c63d10ecc4b754fa"
)
MAT = "c" * 64
SPANS = (  # (page, text) in seq order: synthetic, no book text
    (61, "Each block carries a nonce. Miners vary the nonce until the hash of the block header is below a target."),
    (62, "The target is adjusted every 2016 blocks so that one block is found about every ten minutes."),
    (63, "An attacker who is z blocks behind catches up with a probability that falls exponentially in z."),
)


def load_yaml(path: Path) -> Any:
    return YAML(typ="safe").load(path.read_text(encoding="utf-8"))


def spec() -> dict[str, Any]:
    data: dict[str, Any] = load_yaml(SPEC)
    return data


def bounds() -> dict[str, Any]:
    value: dict[str, Any] = spec()["bounds"]
    return value


def fixture(name: str) -> Any:
    return load_yaml(FIXTURES / name)


def need(name: str) -> ModuleType:
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as missing:
        if missing.name and (name == missing.name or name.startswith(missing.name + ".")):
            pytest.fail(f"{name} is not implemented yet (SRV-TUTOR Red)")
        raise


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ---- Postgres --------------------------------------------------------------------------------------------------


def _daemon_up() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False, timeout=20).returncode == 0


@dataclass(frozen=True)
class Pg:
    host: str
    port: int
    user: str
    password: str
    database: str


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


CONFIG = """\
version: 1
instance: { url: "https://localhost", data_dir: ./data }
users:
  - handle: ivan
    role: owner
    providers:
      - { kind: anthropic-subscription, token: "secret://keychain/reader-ai/claude-oauth-token" }
  - handle: anna
    role: learner
    providers:
      - { kind: anthropic-subscription, token: "secret://keychain/reader-ai/anna-token" }
tasks:
  tutor: { model: sonnet }
budgets: { monthly_usd: 200, warn_at: 0.8 }
sharing: {}
"""


@dataclass
class World:
    engine: Any
    settings: Any
    config: Any
    blobs: Path
    users: dict[str, uuid.UUID]
    lessons: dict[str, uuid.UUID] = field(default_factory=dict)
    revisions: dict[str, str] = field(default_factory=dict)


async def world(pg: Pg, tmp: Path, spans: dict[str, Sequence[tuple[int, str]]] | None = None) -> World:
    """Migrated database; ivan and anna each own a course bound to material MAT with one lesson (F01) and spans
    (SPANS, or `spans[handle]`: a sealed pack's spans are immutable, so a test chooses them here)."""
    from sqlalchemy import text

    from reader.auth.policy import settings_from_config
    from reader.auth.store import prepare
    from reader.config import Secret, load_config
    from reader.db import DbTarget, make_engine, migrate
    from reader.lessons import store

    path = tmp / "reader.yaml"
    path.write_text(textwrap.dedent(CONFIG), encoding="utf-8")
    config = load_config(path)
    settings = settings_from_config(config)
    engine = make_engine(DbTarget(pg.host, pg.port, pg.user, pg.database, Secret(pg.password)))
    await migrate(engine)
    await prepare(engine, settings)
    blobs = tmp / "blobs"
    blobs.mkdir()
    async with engine.connect() as conn:
        users = {r[0]: r[1] for r in await conn.execute(text("SELECT handle, id FROM users"))}
    w = World(engine, settings, config, blobs, users)
    for handle in ("ivan", "anna"):
        owner = users[handle]
        async with engine.begin() as conn:
            course = await store.create_course(conn, owner, f"Book of {handle}", MAT)
            lesson = await store.ensure_lesson(conn, owner, course, 1, "Lesson")
            sha, _ = await store.write_revision(conn, blobs, owner, lesson, F01, "draft", {})
        w.lessons[handle], w.revisions[handle] = lesson, sha
        await add_material(w, handle, MAT, (spans or {}).get(handle, SPANS))
    return w


async def add_material(w: World, handle: str, sha: str, spans: Sequence[tuple[int, str]]) -> uuid.UUID:
    from sqlalchemy import text

    owner = w.users[handle]
    async with w.engine.begin() as conn:
        material = (
            await conn.execute(
                text(
                    "INSERT INTO materials (owner_id, sha256, config_id, page_count)"
                    " VALUES (:o, :s, 'book', 500) RETURNING id"
                ),
                {"o": owner, "s": sha},
            )
        ).scalar_one()
        ingest = (
            await conn.execute(
                text(
                    "INSERT INTO ingests (owner_id, material_id, converter_version, artifact_id, span_count,"
                    " outline_count, outline_source, pages_without_text)"
                    " VALUES (:o, :m, 'test-1', :a, :n, 0, 'headings', '{}') RETURNING id"
                ),
                {"o": owner, "m": material, "a": "d" * 64, "n": len(spans)},
            )
        ).scalar_one()
        for seq, (page, body) in enumerate(spans):
            await conn.execute(
                text(
                    "INSERT INTO material_spans (ingest_id, owner_id, seq, page, kind, text, x0, y0, x1, y1)"
                    " VALUES (:i, :o, :q, :p, 'text', :t, 0, 0, 1, 1)"
                ),
                {"i": ingest, "o": owner, "q": seq, "p": page, "t": body},
            )
        from ingest.packseed import seal  # FR-TUTOR-5: spans are read from the material's active pack only

        await seal(conn, owner, ingest, sha)
    result: uuid.UUID = ingest
    return result


async def scalar(engine: Any, sql: str, **params: Any) -> Any:
    from sqlalchemy import text

    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar()


async def rows(engine: Any, sql: str, **params: Any) -> list[Any]:
    from sqlalchemy import text

    async with engine.connect() as conn:
        return list((await conn.execute(text(sql), params)).all())


async def execute(engine: Any, sql: str, **params: Any) -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text(sql), params)


# ---- fakes -----------------------------------------------------------------------------------------------------


async def locators(conn: Any, owner_id: uuid.UUID, material_sha256: str) -> dict[int, str]:
    """seq → locator of the owner's active pack of the material (the ids `find` returns)."""
    from reader.ingest import views

    pack = await views.active_pack(conn, owner_id, material_sha256)
    assert pack is not None
    return {s.seq: str(s.locator) for s in await views.pack_spans(conn, pack)}


@dataclass
class FakeFind:
    """SRV-SEARCH's find(caller, channel, query, *, deps, …) (FR-SEARCH-22) over the caller's active pack of the
    corpus's material scope: one L1 card per `cards` entry (seq, next seq or None), its id the span's locator and
    its `next` the next span's locator then a section id, as StoreCorpus serves them. `raw` gives the cards as
    they are instead (ids that are not the pack's, other layers). Every call is recorded with its keywords."""

    cards: list[tuple[int, int | None]] = field(default_factory=lambda: [(0, 1), (1, 2), (2, None)])
    degraded: bool = False
    raw: list[dict[str, Any]] | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, caller: Any, channel: str, query: str, **kw: Any) -> dict[str, Any]:
        self.calls.append({"caller": caller, "channel": channel, "query": query, **kw})
        if self.raw is not None:
            return {"cards": self.raw, "degraded": self.degraded}
        corpus = kw["deps"].corpus
        ids = await locators(corpus.conn, caller.user_id, corpus.material_sha256)
        section = f"sec:{corpus.material_sha256}:1"
        cards = [{"id": ids[seq], "layer": "L1", "next": ([] if nxt is None else [ids[nxt]]) + [section]}
                 for seq, nxt in self.cards]
        return {"cards": cards, "degraded": self.degraded}


def answer_json(blocks: list[dict[str, Any]] | None = None, insufficient: bool = False) -> str:
    import json

    default = [{"text": "Miners search for a nonce.", "spans": ["s1"], "quote": "Miners vary the nonce until the hash"}]
    return json.dumps({"blocks": default if blocks is None else blocks, "insufficient": insufficient})


@dataclass
class Reply:
    """One scripted Provider result: text and stop reason, or an SRV-AGENT error code to raise."""

    text: str = field(default_factory=answer_json)
    code: str | None = None
    stop_reason: str | None = "end_turn"
    sdk_usd: Decimal = Decimal("0.05")


@dataclass
class FakeProvider:
    """The SRV-AGENT Provider port (FR-AGENT-6, -12, -13): records every StageCall, returns scripted replies."""

    replies: list[Reply] = field(default_factory=list)
    calls: list[Any] = field(default_factory=list)

    async def run(self, call: Any) -> Any:
        from reader.agent.errors import agent_error
        from reader.agent.pricing import Usage
        from reader.agent.provider import StageResult

        self.calls.append(call)
        reply = self.replies.pop(0) if self.replies else Reply()
        if reply.code is not None:
            raise agent_error(reply.code)
        usage = Usage(input=2, output=200, cache_write_5m=0, cache_write_1h=900, cache_read=0)
        return StageResult(reply.text, usage, reply.sdk_usd, "claude-sonnet-5-5", reply.stop_reason)


def policy_pair() -> tuple[Any, str]:
    """SRV-SEARCH's shipped policy and its version without the judge slots' config (the process's `current()`
    reads reader.yaml from READER_CONFIG, which the tests do not set)."""
    from reader.search import policy

    loaded = policy.policy()
    return loaded, policy.policy_version(loaded, slots={})


def deps(w: World, provider: Any, find: Any | None = None, **kw: Any) -> Any:
    jobs = need("reader.tutor.jobs")
    return jobs.Deps(
        provider=lambda _engine: contextlib.nullcontext(provider),
        find=find or FakeFind(),
        blobs_root=w.blobs,
        config=lambda: w.config,
        **{"policy": policy_pair, **kw},
    )


async def run_handler(w: World, question_id: uuid.UUID, d: Any, attempts: int = 0) -> None:
    """Run the `tutor.answer` handler once inside one transaction, as the SRV-EVENTS wrapper does."""
    jobs = need("reader.tutor.jobs")
    from reader.jobs.model import JobContext

    kind = jobs.make_kind(d)
    args = kind.args.model_validate({"question_id": str(question_id)})
    async with w.engine.connect() as conn, conn.begin() as tx:
        try:
            await kind.handler(args, conn, JobContext(job_id=1, key="test", attempts=attempts))
        except BaseException:
            await tx.rollback()
            raise
        await tx.commit()


async def insert_question(
    w: World, handle: str = "ivan", question: str = "Why do miners vary the nonce?", cell_id: str | None = None,
    age_s: int = 0,
) -> uuid.UUID:
    store = need("reader.tutor.store")
    async with w.engine.begin() as conn:
        qid: uuid.UUID = await store.insert_question(
            conn, w.users[handle], w.lessons[handle], w.revisions[handle], cell_id, question
        )
    if age_s:
        await execute(
            w.engine, "UPDATE tutor_questions SET created_at = now() - make_interval(secs => :a) WHERE id = :i",
            a=age_s, i=qid,
        )
    return qid


async def question_row(w: World, qid: uuid.UUID) -> Any:
    found = await rows(
        w.engine,
        "SELECT state, answer, grounded, error_code, model FROM tutor_questions WHERE id = :i",
        i=qid,
    )
    return found[0] if found else None


# ---- api -------------------------------------------------------------------------------------------------------


async def session_for(w: World, handle: str) -> str:
    from reader.auth.store import _start_session

    async with w.engine.begin() as conn:
        return await _start_session(conn, w.users[handle], None)


@dataclass
class FakeJobs:
    """The api's SRV-EVENTS `Jobs` (enqueue, job_status), recording enqueues; `fail` makes enqueue raise."""

    enqueued: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    statuses: dict[str, str] = field(default_factory=dict)
    fail: bool = False

    async def enqueue(self, kind_name: str, args: Any, schedule_in: int = 0) -> Any:
        if self.fail:
            raise RuntimeError("queue down")
        self.enqueued.append((kind_name, dict(args)))
        return type("Result", (), {"kind": "queued", "job_id": len(self.enqueued)})()

    async def job_status(self, kind_name: str, key: str) -> str:
        return self.statuses.get(key, "none")


@asynccontextmanager
async def client(w: World, handle: str | None = "ivan", jobs: Any = None) -> AsyncIterator[httpx.AsyncClient]:
    from reader.api.app import create_app

    app = create_app(w.settings, w.engine, run_cleanup_task=False, jobs=jobs)
    app.state.blobs_root = w.blobs
    cookies = {SESSION_COOKIE: await session_for(w, handle)} if handle else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url=ORIGIN, headers={"Origin": ORIGIN}, cookies=cookies
    ) as c:
        yield c
