"""Shared fixtures for SRV-FRESH tests: the spec and its vectors, a migrated database with materials,
spans and an outline, a fake Provider port that writes real ledger rows and drives the search gate,
fake Jobs, and an api client with a session.

The module under test (`reader.freshness`) is imported only through `need()` inside tests, so a missing module
fails each test (Red) instead of the collection.
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
FIXTURES = REPO / "spec" / "fixtures" / "SRV-FRESH"
SPEC = REPO / "spec" / "modules" / "SRV-FRESH.yaml"
ORIGIN = "https://localhost"
SESSION_COOKIE = "__Host-reader_session"
PG_IMAGE = (
    "pgvector/pgvector:0.8.7-pg18-trixie@sha256:ab49524d18df7200b74ac9a475e1db6297fc1eb57da4c214c63d10ecc4b754fa"
)
MAT = "e" * 64
# Synthetic spans (no book text): [page, kind, text] in seq order. Pages 1-2 are the front; the rest are body.
SPANS: tuple[tuple[int, str, str], ...] = (
    (1, "heading", "Practical Distributed Ledgers"),
    (2, "text", "Second edition. Example Press, 2017."),
    (8, "text", "The consensus protocol version 1.0 was released in 2016 and is the latest one."),
    (9, "text", "Plain words about ledgers and blocks without dates."),
    (15, "list_item", "The client library 2.3 API is deprecated in newer releases."),
    (16, "code", "client.connect(version=2.3)"),
)
OUTLINE: tuple[tuple[int, str, int], ...] = ((1, "Ledgers", 7), (2, "Blocks", 9), (1, "Clients", 14))


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
            pytest.fail(f"{name} is not implemented yet (SRV-FRESH Red)")
        raise


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ---- Postgres --------------------------------------------------------------------------------------------


def _daemon_up() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False, timeout=20).returncode == 0  # noqa: S607


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


CONFIG = f"""\
version: 1
instance: {{ url: "https://localhost", data_dir: ./data }}
users:
  - handle: ivan
    role: owner
    providers:
      - {{ kind: anthropic-subscription, token: "secret://keychain/reader-ai/claude-oauth-token" }}
  - handle: anna
    role: learner
    providers:
      - {{ kind: anthropic-subscription, token: "secret://keychain/reader-ai/anna-token" }}
tasks:
  freshness_scan: {{ model: sonnet }}
  freshness_web: {{ model: haiku }}
  freshness_report: {{ model: sonnet }}
budgets: {{ monthly_usd: 200, warn_at: 0.8 }}
materials:
  - {{ id: book, file: ./book.pdf, sha256: "{MAT}", license: personal-use, language: en }}
sharing: {{}}
"""


@dataclass
class World:
    engine: Any
    settings: Any
    config: Any
    users: dict[str, uuid.UUID]
    materials: dict[str, uuid.UUID] = field(default_factory=dict)
    ingests: dict[str, uuid.UUID] = field(default_factory=dict)


async def world(pg: Pg, tmp: Path) -> World:
    """Migrated database; ivan (owner) and anna (learner) each own material MAT with spans and an outline."""
    from sqlalchemy import text

    from reader.auth.policy import settings_from_config
    from reader.auth.store import prepare
    from reader.config import Secret, load_config
    from reader.db import DbTarget, make_engine, migrate

    path = tmp / "reader.yaml"
    path.write_text(textwrap.dedent(CONFIG), encoding="utf-8")
    config = load_config(path)
    settings = settings_from_config(config)
    engine = make_engine(DbTarget(pg.host, pg.port, pg.user, pg.database, Secret(pg.password)))
    await migrate(engine)
    await prepare(engine, settings)
    async with engine.connect() as conn:
        users = {r[0]: r[1] for r in await conn.execute(text("SELECT handle, id FROM users"))}
    w = World(engine, settings, config, users)
    for handle in ("ivan", "anna"):
        material, ingest = await add_material(w, handle, MAT, SPANS, OUTLINE)
        w.materials[handle], w.ingests[handle] = material, ingest
    return w


async def add_material(
    w: World,
    handle: str,
    sha: str,
    spans: Sequence[tuple[int, str, str]],
    outline: Sequence[tuple[int, str, int]],
    *,
    ingested: bool = True,
    config_id: str = "book",
) -> tuple[uuid.UUID, uuid.UUID | None]:
    from sqlalchemy import text

    owner = w.users[handle]
    async with w.engine.begin() as conn:
        material = (
            await conn.execute(
                text(
                    "INSERT INTO materials (owner_id, sha256, config_id, page_count)"
                    " VALUES (:o, :s, :c, 40) RETURNING id"
                ),
                {"o": owner, "s": sha, "c": config_id},
            )
        ).scalar_one()
        if not ingested:
            return material, None
        ingest = (
            await conn.execute(
                text(
                    "INSERT INTO ingests (owner_id, material_id, converter_version, artifact_id, span_count,"
                    " outline_count, outline_source, pages_without_text)"
                    " VALUES (:o, :m, 'test-1', :a, :n, :k, 'bookmarks', '{}') RETURNING id"
                ),
                {"o": owner, "m": material, "a": "d" * 64, "n": len(spans), "k": len(outline)},
            )
        ).scalar_one()
        for seq, (page, kind, body) in enumerate(spans):
            await conn.execute(
                text(
                    "INSERT INTO material_spans (ingest_id, owner_id, seq, page, kind, text, x0, y0, x1, y1)"
                    " VALUES (:i, :o, :q, :p, :k, :t, 0, 0, 1, 1)"
                ),
                {"i": ingest, "o": owner, "q": seq, "p": page, "k": kind, "t": body},
            )
        for seq, (level, title, page) in enumerate(outline):
            await conn.execute(
                text(
                    "INSERT INTO material_outline (ingest_id, owner_id, seq, level, title, page)"
                    " VALUES (:i, :o, :q, :l, :t, :p)"
                ),
                {"i": ingest, "o": owner, "q": seq, "l": level, "t": title, "p": page},
            )
    return material, ingest


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


# ---- fakes -----------------------------------------------------------------------------------------------


def search_result(
    query: str, links: Sequence[tuple[str, str]], summary: str = "A short summary."
) -> dict[str, Any]:
    """A UserMessage.tool_use_result in the measured shape (spikes/web-search section 3a)."""
    content = [{"title": t, "url": u} for t, u in links]
    return {
        "query": query,
        "results": [{"tool_use_id": "srvtoolu_1", "content": content}, summary],
        "durationSeconds": 1.0,
        "searchCount": 1,
    }


@dataclass
class Reply:
    """One scripted Provider result for a stage, or an SRV-AGENT error code to raise.

    `paid` decides the ledger row: the reserve settles at `sdk_usd` (paid) or $0. For a search,
    `tool_input` is what the fake CLI passes to the PreToolUse hook (None: the planned query verbatim); when
    the hook allows it, the run returns `links` as its structured result.
    """

    text: str = ""
    code: str | None = None
    sdk_usd: Decimal = Decimal("0.01")
    paid: bool = True
    links: Sequence[tuple[str, str]] = ()
    summary: str = "A short summary."
    tool_input: dict[str, Any] | None = None


@dataclass
class FakeProvider:
    """The SRV-AGENT Provider port: records every StageCall, writes reserve and settle rows through the real
    ledger, drives a search call's hook, and returns scripted replies per stage (scan, search, report)."""

    engine: Any
    replies: dict[str, list[Reply]] = field(default_factory=dict)
    calls: list[Any] = field(default_factory=list)
    allowed_queries: list[str] = field(default_factory=list)
    hook_outputs: list[dict[str, Any]] = field(default_factory=list)

    async def run(self, call: Any) -> Any:
        from reader.agent import ledger
        from reader.agent.errors import agent_error
        from reader.agent.pricing import Usage
        from reader.agent.provider import StageResult

        self.calls.append(call)
        queue = self.replies.get(call.stage, [])
        reply = queue.pop(0) if queue else Reply()
        model = "claude-haiku-5-5" if call.stage == "search" else "claude-sonnet-5-5"
        scope = ledger.Scope(call.owner_id, call.run_id, call.lesson_id, call.stage, call.task, call.attempt)
        caps = ledger.Caps(call.run_cap_usd, None, None, None, call.daily_budget_usd)
        rid = await ledger.reserve(self.engine, scope, model, Decimal("0.01"), caps)
        settled = reply.sdk_usd if reply.paid else Decimal(0)
        record = ledger.UpstreamRecord((200,) if reply.paid else (), ())
        tool_results: tuple[Any, ...] = ()
        searches = 0
        if call.web_search is not None:
            query = call.prompt.removeprefix("Query: ")
            tool_input = reply.tool_input if reply.tool_input is not None else {"query": query}
            out = await call.web_search.hook({"tool_name": "WebSearch", "tool_input": tool_input}, "t1", None)
            self.hook_outputs.append(out)
            if out["hookSpecificOutput"]["permissionDecision"] == "allow":
                self.allowed_queries.append(str(tool_input.get("query")))
                tool_results = (search_result(query, reply.links, reply.summary),)
                searches = 1
        usage = Usage(2, 200, 0, 900, 0, None, searches if call.web_search is not None else None)
        await ledger.settle(self.engine, rid, settled, settled, settled, usage, record)
        if reply.code is not None:
            raise agent_error(reply.code)
        return StageResult(reply.text, usage, settled, model, "end_turn", tool_results)


@dataclass
class FakeJobs:
    """The SRV-EVENTS `Jobs` the routes and the scan job enqueue through; records enqueues."""

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


def deps(w: World, provider: Any, jobs: Any = None, today: Any = None) -> Any:
    from datetime import date

    fjobs = need("reader.freshness.jobs")
    return fjobs.Deps(
        provider=lambda _engine: contextlib.nullcontext(provider),
        config=lambda: w.config,
        jobs=lambda: jobs if jobs is not None else FakeJobs(),
        today=today or (lambda: date(2026, 11, 23)),
    )


async def run_job(w: World, kind_name: str, report_id: uuid.UUID, d: Any, attempts: int = 0) -> None:
    """Run a freshness job's handler once inside one transaction, as the SRV-EVENTS wrapper does."""
    fjobs = need("reader.freshness.jobs")
    from reader.jobs.model import JobContext

    kind = next(k for k in fjobs.make_kinds(d) if k.name == kind_name)
    args = kind.args.model_validate({"report_id": str(report_id)})
    async with w.engine.connect() as conn, conn.begin() as tx:
        try:
            await kind.handler(args, conn, JobContext(job_id=1, key="test", attempts=attempts))
        except BaseException:
            await tx.rollback()
            raise
        await tx.commit()


async def session_for(w: World, handle: str) -> str:
    from reader.auth.store import _start_session

    async with w.engine.begin() as conn:
        return await _start_session(conn, w.users[handle], None)


@asynccontextmanager
async def client(w: World, handle: str | None = "ivan", jobs: Any = None) -> AsyncIterator[httpx.AsyncClient]:
    from reader.api.app import create_app

    app = create_app(w.settings, w.engine, run_cleanup_task=False, jobs=jobs)
    app.state.freshness_config = w.config
    cookies = {SESSION_COOKIE: await session_for(w, handle)} if handle else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url=ORIGIN, headers={"Origin": ORIGIN}, cookies=cookies
    ) as c:
        yield c
