"""Fixtures and helpers for the SRV-PIPELINE tests (spec/modules/SRV-PIPELINE.yaml).

@trace VEC-PIPELINE-2

Modules under test are imported through `need()` inside test bodies (Red phase: an assertion failure, not a
collection error). [db] tests run the real job machinery (SRV-EVENTS, procrastinate) on the Postgres image OPS-DEPLOY
pins, with SRV-AGENT's real ledger and provider adapter in front of a fake CLI (no credit), a fake span reader and a
fake lab verifier. Model outputs are generated from the decision-table cases (VEC-PIPELINE-2).
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
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from ruamel.yaml import YAML

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "spec" / "fixtures" / "SRV-PIPELINE"
LESSON_FIXTURES = REPO / "spec" / "fixtures" / "SRV-LESSON"
SPEC = REPO / "spec" / "modules" / "SRV-PIPELINE.yaml"
F01 = (LESSON_FIXTURES / "F01.ipynb").read_bytes()
# postgres:18-trixie plus pgvector 0.8.7: the heads include SRV-SEARCH's migration (CREATE EXTENSION vector).
PG_IMAGE = (
    "pgvector/pgvector:0.8.7-pg18-trixie@sha256:ab49524d18df7200b74ac9a475e1db6297fc1eb57da4c214c63d10ecc4b754fa"
)
ORIGIN = "https://localhost"
SESSION_COOKIE = "__Host-reader_session"
MATERIAL = "ab" * 32
CANARY = "CANARY-SPAN-TEXT-5d1c"
# VEC-PIPELINE-2's fixed reserves, used as stage floors so the fake prompts' small estimates never decide.
BY_NAME_HINT = {"draft": 0.94, "fact_check": 0.38, "adversarial_review": 0.75, "revise": 1.14}


def load_yaml(path: Path) -> Any:
    return YAML(typ="safe").load(path.read_text(encoding="utf-8"))


def spec_bounds() -> dict[str, Any]:
    data: dict[str, Any] = load_yaml(SPEC)["bounds"]
    return data


def need(name: str) -> ModuleType:
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as error:
        raise AssertionError(f"{name} is not implemented ({error.name} missing)") from None


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def apply_ops(doc: Any, ops: list[dict[str, Any]]) -> Any:
    """JSON Patch subset (add, remove, replace, move), as SRV-LESSON's fixtures use it."""

    def parts(pointer: str) -> list[str]:
        return [p.replace("~1", "/").replace("~0", "~") for p in pointer.split("/")[1:]]

    def parent(node: Any, pointer: str) -> tuple[Any, str]:
        *head, last = parts(pointer)
        for part in head:
            node = node[int(part)] if isinstance(node, list) else node[part]
        return node, last

    def remove(node: Any, pointer: str) -> Any:
        container, last = parent(node, pointer)
        return container.pop(int(last)) if isinstance(container, list) else container.pop(last)

    def add(node: Any, pointer: str, value: Any) -> None:
        container, last = parent(node, pointer)
        if isinstance(container, list):
            container.insert(len(container) if last == "-" else int(last), value)
        else:
            container[last] = value

    doc = copy.deepcopy(doc)
    for op in ops:
        if op["op"] == "remove":
            remove(doc, op["path"])
        elif op["op"] == "add":
            add(doc, op["path"], copy.deepcopy(op["value"]))
        elif op["op"] == "replace":
            container, last = parent(doc, op["path"])
            container[int(last) if isinstance(container, list) else last] = copy.deepcopy(op["value"])
        elif op["op"] == "move":
            add(doc, op["path"], remove(doc, op["from"]))
    return doc


# --- spans ---------------------------------------------------------------------------------------------------------


def synthetic_spans(n: int = 6, extra: str = "") -> list[Any]:
    source = need("reader.pipeline.source")
    return [
        source.Span(f"{MATERIAL}:{i}", 61 + i // 3, "text", f"Paragraph {i} about consensus. {CANARY}{extra}")
        for i in range(n)
    ]


@dataclass
class FakeSpans:
    """The SRV-INGEST span reader, in memory."""

    spans: list[Any]
    title: str = "Synthetic material"

    async def load(self, conn: Any, owner_id: uuid.UUID, material_sha256: str, first: int, last: int) -> Any:
        return [s for s in self.spans if first <= s.page <= last], self.title


@dataclass
class FakeVerify:
    """Lab verification outcome per case: pass, assert_fails or none."""

    mode: str = "pass"
    calls: int = 0

    async def run(self, notebook: dict[str, Any]) -> Any:
        verify = need("reader.pipeline.verify")
        findings = need("reader.pipeline.findings")
        self.calls += 1
        results: dict[str, Any] = {}
        problems: list[Any] = []
        for _, cell in findings.labs_of(notebook):
            passed = self.mode == "pass"
            results[cell["id"]] = {
                "runs": 2,
                "passed": passed,
                "outputs_identical": True,
                "input_sha256": findings.source_sha256(cell),
            }
            if not passed:
                problems.append(
                    findings.Problem(
                        "lab_failed", cell["id"], None, "lab assert failed", "lab_verification", True
                    )
                )
        return verify.LabReport(findings.labs_sha256(notebook), results, problems)


# --- model outputs generated from a case ---------------------------------------------------------------------------

CLAIMS = 6
_COUNTER = [0]


def draft_output(labs: str) -> str:
    _COUNTER[0] += 1
    cells: list[dict[str, Any]] = [
        {
            "id": "e1",
            "kind": "explain",
            "sectors": ["consensus"],
            "source": f"## Consensus\n\nVersion {_COUNTER[0]}.",
            "claims": [
                {"id": f"c{i}", "text": f"Claim {i}.", "citations": [{"span": f"s{(i % 3) + 1}"}]}
                for i in range(1, CLAIMS + 1)
            ],
        },
        {"id": "notes", "kind": "notes", "source": "## Your notes\n"},
    ]
    if labs != "none":
        cells.insert(
            1,
            {
                "id": "lab1",
                "kind": "lab",
                "source": "x = 21 * 2\nprint(x)",
                "kernel": "python3",
                "network": "none",
                "asserts": ["x == 42"],
            },
        )
    return json.dumps({"cells": cells})


def fact_output(unsupported: int, contradicted: int) -> str:
    verdicts = []
    for i in range(1, CLAIMS + 1):
        verdict = (
            "unsupported"
            if i <= unsupported
            else ("contradicted" if i <= unsupported + contradicted else "supported")
        )
        verdicts.append(
            {
                "cell": "e1",
                "claim": f"c{i}",
                "verdict": verdict,
                "spans": [] if verdict == "unsupported" else [f"s{(i % 3) + 1}"],
                "note": f"n{i}",
            }
        )
    return json.dumps({"verdicts": verdicts})


def review_output(blocking: int, nonblocking: int) -> str:
    _COUNTER[0] += 1
    tag = _COUNTER[0]
    findings = [
        {"cell": "e1", "claim": None, "blocking": True, "text": f"blocking {i} ({tag})"}
        for i in range(blocking)
    ]
    findings += [
        {"cell": None, "claim": None, "blocking": False, "text": f"minor {i} ({tag})"}
        for i in range(nonblocking)
    ]
    return json.dumps({"findings": findings})


def scripts_for(results: list[dict[str, Any]], labs: str) -> list[Any]:
    from agent.conftest import (
        Script,
    )  # the fake CLI's script type (tests/ is on sys.path: rootdir-relative packages)

    out = []
    for r in results:
        stage = r["stage"]
        if r.get("error") == "rate_limited":
            up = r["upstream"]
            out.append(
                Script(
                    text="",
                    cost=0,
                    assistant_error="rate_limit",
                    is_error=True,
                    statuses=tuple(up["statuses"]),
                )
            )
            continue
        if r.get("invalid") == "summary_only":
            text = json.dumps({"all_supported": True, "count": CLAIMS})
        elif stage in ("draft", "revise"):
            text = draft_output(labs)
        elif stage == "fact_check":
            text = fact_output(r.get("unsupported", 0), r.get("contradicted", 0))
        else:
            text = review_output(r.get("blocking", 0), r.get("nonblocking", 0))
        out.append(Script(text=text, cost=r["cost"]))
    return out


# --- Postgres, jobs, runtime ---------------------------------------------------------------------------------------


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


def fresh_pg(pg_container: tuple[Pg, Any]) -> Pg:
    server, container = pg_container
    name = "p_" + secrets.token_hex(6)
    code, out = container.exec(["psql", "-U", "postgres", "-c", f"CREATE DATABASE {name}"])
    assert code == 0, out
    return Pg(server.host, server.port, server.user, server.password, name)


@pytest.fixture
def pg(pg_container: tuple[Pg, Any]) -> Pg:
    return fresh_pg(pg_container)


def config_text(task_budgets: dict[str, float] | None = None) -> str:
    b = task_budgets or {"draft": 4.0, "fact_check": 2.0, "review": 4.0}
    return textwrap.dedent(f"""\
        version: 1
        instance: {{ url: "https://localhost", data_dir: ./data }}
        users:
          - handle: ivan
            role: owner
            providers:
              - {{ kind: anthropic-subscription, token: "secret://keychain/reader-ai/claude-oauth-token" }}
          - {{ handle: anna, role: learner }}
        tasks:
          draft:      {{ model: opus,   budget_usd_per_lesson: {b["draft"]} }}
          fact_check: {{ model: sonnet, budget_usd_per_lesson: {b["fact_check"]} }}
          review:     {{ model: opus,   budget_usd_per_lesson: {b["review"]} }}
        budgets: {{ monthly_usd: 1000, warn_at: 0.8 }}
        sharing: {{}}
        """)


# SRV-INGEST's tables (its FR-INGEST-14), created by the test where SRV-INGEST's migration is not merged yet.
INGEST_DDL = (
    """CREATE TABLE IF NOT EXISTS materials (id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        owner_id uuid NOT NULL REFERENCES users (id), sha256 text NOT NULL, config_id text, page_count int,
        created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (owner_id, sha256))""",
    """CREATE TABLE IF NOT EXISTS ingests (id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        owner_id uuid NOT NULL REFERENCES users (id), material_id uuid NOT NULL REFERENCES materials (id),
        converter_version text, artifact_id text, span_count int, outline_count int, outline_source text,
        pages_without_text int[], imported_at timestamptz NOT NULL DEFAULT now(), UNIQUE (material_id))""",
    """CREATE TABLE IF NOT EXISTS material_spans (ingest_id uuid NOT NULL REFERENCES ingests (id),
        owner_id uuid NOT NULL REFERENCES users (id), seq int NOT NULL, page int NOT NULL, kind text NOT NULL,
        level int, decoded bool, text text NOT NULL, x0 real, y0 real, x1 real, y1 real, PRIMARY KEY (ingest_id, seq))""",
)


async def import_material(engine: Any, owner: uuid.UUID, spans: list[Any], sha: str = MATERIAL) -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        for ddl in INGEST_DDL:
            await conn.execute(text(ddl))
        mid = (
            await conn.execute(
                text(
                    "INSERT INTO materials (owner_id, sha256, config_id, page_count) VALUES (:o, :s, 'synthetic-1', 100)"
                    " RETURNING id"
                ),
                {"o": owner, "s": sha},
            )
        ).scalar_one()
        iid = (
            await conn.execute(
                text(
                    "INSERT INTO ingests (owner_id, material_id, converter_version, artifact_id, span_count,"
                    " outline_count, outline_source, pages_without_text)"
                    " VALUES (:o, :m, 'v1', :a, :n, 0, 'headings', '{}') RETURNING id"
                ),
                {"o": owner, "m": mid, "a": "d" * 64, "n": len(spans)},
            )
        ).scalar_one()
        for s in spans:
            seq = int(s.span_id.split(":")[1])
            await conn.execute(
                text(
                    "INSERT INTO material_spans (ingest_id, owner_id, seq, page, kind, text, x0, y0, x1, y1)"
                    " VALUES (:i, :o, :q, :p, :k, :t, 0, 0, 1, 1)"
                ),
                {"i": iid, "o": owner, "q": seq, "p": s.page, "k": s.kind, "t": s.text},
            )


def plan(lesson_pages: tuple[int, int] = (61, 62)) -> dict[str, Any]:
    return {
        "course": {"id": "synthetic", "title": "Synthetic course", "material_sha256": MATERIAL},
        "lessons": [
            {"id": "L01", "title": "First", "chapter": 1, "pages": {"pdf": [60, 60]}},
            {"id": "L02", "title": "Consensus", "chapter": 2, "pages": {"pdf": list(lesson_pages)}},
        ],
    }


@dataclass
class Env:
    pg: Pg
    engine: Any
    app: Any
    jobs: Any
    config: Any
    users: dict[str, uuid.UUID]
    blobs: Path
    cli: Any
    proxy: Any
    spans: FakeSpans
    verify: FakeVerify
    runtime: Any = None
    lesson: uuid.UUID | None = None

    async def scalar(self, sql: str, **params: Any) -> Any:
        from sqlalchemy import text

        async with self.engine.connect() as conn:
            return (await conn.execute(text(sql), params)).scalar()

    async def rows(self, sql: str, **params: Any) -> list[Any]:
        from sqlalchemy import text

        async with self.engine.connect() as conn:
            return list((await conn.execute(text(sql), params)).all())

    async def work(self) -> None:
        await self.app.run_worker_async(
            queues=self.jobs.queues,
            wait=False,
            concurrency=1,
            install_signal_handlers=False,
            listen_notify=False,
        )

    async def import_plan(self) -> uuid.UUID:
        courses = need("reader.pipeline.courses")
        async with self.engine.begin() as conn:
            await courses.import_plan(conn, self.users["ivan"], plan())
        lesson = await self.scalar(
            "SELECT lesson_id FROM course_plans WHERE plan_lesson_id = 'L02' AND owner_id = :o",
            o=self.users["ivan"],
        )
        self.lesson = lesson
        return lesson  # type: ignore[no-any-return]

    async def compile(self, cap: str = "8.00") -> uuid.UUID:
        runs = need("reader.pipeline.runs")
        assert self.lesson is not None
        run: uuid.UUID = await runs.create_run(
            self.engine, self.jobs, self.users["ivan"], self.lesson, Decimal(cap)
        )
        return run

    async def state(self, run: uuid.UUID) -> dict[str, Any]:
        runs = need("reader.pipeline.runs")
        status: dict[str, Any] = await runs.status(self.engine, self.jobs, self.users["ivan"], run)
        return status

    async def notebook(self, sha256: str) -> dict[str, Any]:
        from reader import blobs

        data: dict[str, Any] = json.loads(blobs.get(self.blobs, sha256))
        return data


def target(pg: Pg) -> Any:
    from reader.config import Secret
    from reader.db import DbTarget

    return DbTarget(pg.host, pg.port, pg.user, pg.database, Secret(pg.password))


@asynccontextmanager
async def pipeline_env(
    pg: Pg,
    tmp: Path,
    *,
    task_budgets: dict[str, float] | None = None,
    labs: str = "pass",
    floors: dict[str, float] | None = None,
) -> AsyncIterator[Env]:
    """A migrated database with users, the pipeline's job kinds on a real procrastinate app, and a configured
    runtime: AgentProvider (real ledger) → FakeCli, FakeSpans, FakeVerify."""
    from sqlalchemy import text

    from agent.conftest import FakeCli, FakeProxyClient
    from reader.auth.policy import settings_from_config
    from reader.auth.store import prepare
    from reader.config import load_config
    from reader.db import make_engine, migrate
    from reader.jobs import Catalog
    from reader.jobs.queue import Jobs
    from reader.jobs.worker import make_app

    jobs_mod = need("reader.pipeline.jobs")
    stages = need("reader.pipeline.stages")
    prompts = need("reader.pipeline.prompts")
    provider_mod = need("reader.agent.provider")

    path = tmp / "reader.yaml"
    path.write_text(config_text(task_budgets), encoding="utf-8")
    config = load_config(path)
    engine = make_engine(target(pg))
    await migrate(engine)
    await prepare(engine, settings_from_config(config))
    async with engine.connect() as conn:
        users = {r[0]: r[1] for r in await conn.execute(text("SELECT handle, id FROM users"))}
    app = make_app(target(pg))
    await app.open_async()
    blobs = tmp / "blobs"
    blobs.mkdir()
    homes = tmp / "homes"
    homes.mkdir()
    cli = FakeCli([])
    proxy = FakeProxyClient(cli)
    spans = FakeSpans(synthetic_spans())
    verify = FakeVerify(labs)
    try:
        jobs = Jobs(engine, app, Catalog(jobs_mod.JOB_KINDS), (), retry_delay=lambda _attempts: 0)
        pipeline = stages.load()
        if floors:
            pipeline = stages.with_floors(pipeline, {k: Decimal(str(v)) for k, v in floors.items()})
        runtime = jobs_mod.Runtime(
            provider=provider_mod.AgentProvider(engine, config, proxy, cli=cli, home_root=homes),
            verify=verify,
            spans=spans,
            blobs_root=blobs,
            pipeline=pipeline,
            prompts=prompts.load(),
            config=config,
            jobs=lambda: jobs,
        )
        jobs_mod.configure(runtime)
        await import_material(engine, users["ivan"], spans.spans)
        env = Env(pg, engine, app, jobs, config, users, blobs, cli, proxy, spans, verify, runtime)
        yield env
    finally:
        jobs_mod.configure(None)
        await app.close_async()
        await engine.dispose()


async def session_cookie(env: Env, handle: str) -> str:
    from reader.auth.store import _start_session

    async with env.engine.begin() as conn:
        return await _start_session(conn, env.users[handle], None)


@asynccontextmanager
async def client(env: Env, handle: str | None = "ivan") -> AsyncIterator[Any]:
    import httpx

    from reader.api.app import create_app
    from reader.auth.policy import settings_from_config

    app = create_app(settings_from_config(env.config), env.engine, run_cleanup_task=False)
    app.state.blobs_root = env.blobs
    app.state.jobs = env.jobs
    cookies = {SESSION_COOKIE: await session_cookie(env, handle)} if handle else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers={"Origin": ORIGIN}, cookies=cookies
    ) as c:
        yield c


@dataclass
class Counter:
    values: list[int] = field(default_factory=list)
