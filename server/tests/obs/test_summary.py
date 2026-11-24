"""SRV-OBS [unit, db]: the owner's views (GET /api/obs/summary) on VEC-OBS-3.

@trace FR-OBS-10 VEC-OBS-3

[unit] VEC-OBS-3's keys are recomputed from their args with reader.jobs.model.idempotency_key and its trace ids with
FR-OBS-5's derivation (this test, not spec/fixtures/SRV-OBS/trace-ids.mjs, is the gate for the fixture); the
nearest-rank percentile. [db] the seeded rows of VEC-OBS-3 give exactly its expected JSON; another owner's rows never
appear. Foreign keys are switched off for the seeding session only (session_replication_role = replica), so the
test seeds just the rows the views read.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from .conftest import FIXTURES, load_yaml, require

VEC = load_yaml(FIXTURES / "summary.yaml")
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
TRACING = SimpleNamespace(**VEC["tracing"])  # the api's settings for VEC-OBS-3's expected result


def _kind(name: str) -> Any:
    from reader.jobs.catalog import load_kinds

    return load_kinds()[name]


def test_vec_obs_3_keys_and_trace_ids_are_derived() -> None:
    from reader.jobs.model import idempotency_key

    ids = require("reader.obs.ids")
    by_job = {}
    for row in VEC["seed"]["job_keys"]:
        assert row["key"] == idempotency_key(_kind(row["kind"]), row["args"]), row["job_id"]
        by_job[row["job_id"]] = row["key"]

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            if "trace_id" in node:
                assert node["trace_id"] == format(
                    ids.derive_trace_id(node["job"], by_job[node["job"]]), "032x"
                )
            if "trace_ids" in node:
                assert node["trace_ids"] == [
                    format(ids.derive_trace_id(j, by_job[j]), "032x") for j in node["jobs"]
                ]
            for value in node.values():
                walk(value)

    walk(VEC["expected"])


@pytest.mark.parametrize(
    ("values", "p", "expected"),
    [
        ([2000, 4000, 9000], 50, 4000),
        ([2000, 4000, 9000], 95, 9000),
        ([5], 95, 5),
        ([], 50, None),
        ([1, 2, 3, 4], 50, 2),
        ([1, 2, 3, 4], 75, 3),
        ([1, 2, 3, 4], 76, 4),
    ],
)
def test_nearest_rank(values: list[int], p: int, expected: int | None) -> None:
    assert require("reader.obs.summary").nearest_rank(values, p) == expected


def _strip(node: Any) -> Any:
    """The fixture's `job`/`jobs` name the seeded row a trace id comes from; they are not part of the JSON."""
    if isinstance(node, list):
        return [_strip(n) for n in node]
    if isinstance(node, dict):
        return {k: _strip(v) for k, v in node.items() if not _row_ref(k, v)}
    return node


def _row_ref(key: str, value: Any) -> bool:
    """`job: 101` or `jobs: [106]` (seeded row ids); `failures.jobs`, a list of groups, stays."""
    if key == "job":
        return isinstance(value, int)
    return key == "jobs" and isinstance(value, list) and all(isinstance(v, int) for v in value)


async def _seed(engine: Any) -> None:
    from sqlalchemy import text

    seed = VEC["seed"]

    def when(value: Any) -> Any:
        """asyncpg binds timestamptz from datetimes only: the fixture's ISO strings become aware datetimes."""
        if isinstance(value, str) and len(value) >= 19 and value[4] == "-" and value[10] == "T":
            return datetime.fromisoformat(value)
        return value

    async def run(statement: Any, params: dict[str, Any]) -> None:
        await conn.execute(statement, {k: when(v) for k, v in params.items()})

    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL session_replication_role = replica"))
        for t in seed["obs_tracing"]:
            await run(
                text("INSERT INTO obs_tracing (started_at, enabled) VALUES (:s, :e)"),
                {"s": t["started_at"], "e": t["enabled"]},
            )
        for r in seed["compile_runs"]:
            await run(
                text(
                    "INSERT INTO compile_runs (id, owner_id, lesson_id, cap_usd, task_budgets, created_at, finished_at)"
                    " VALUES (:id, :o, :l, 8, '{}', :c, :f)"
                ),
                {
                    "id": r["id"],
                    "o": r["owner_id"],
                    "l": r["lesson_id"],
                    "c": r["created_at"],
                    "f": r["finished_at"],
                },
            )
        for s in seed["compile_steps"]:
            owner = next(r["owner_id"] for r in seed["compile_runs"] if r["id"] == s["run_id"])
            await run(
                text(
                    "INSERT INTO compile_steps (owner_id, run_id, step, stage, input_sha256, model, cost_usd, duration_s,"
                    " created_at) VALUES (:o, :r, :n, :st, :i, :m, :c, :d, :at)"
                ),
                {
                    "o": owner,
                    "r": s["run_id"],
                    "n": s["step"],
                    "st": s["stage"],
                    "i": s["input_sha256"],
                    "m": s["model"],
                    "c": Decimal(str(s["cost_usd"])),
                    "d": Decimal(str(s["duration_s"])),
                    "at": "2026-11-24T10:30:00Z",
                },
            )
        for a in seed["agent_spend"]:
            await run(
                text(
                    "INSERT INTO agent_spend (id, owner_id, run_id, stage, task, attempt, kind, reservation_id, model,"
                    " usd, settled_usd, created_at) OVERRIDING SYSTEM VALUE"
                    " VALUES (:id, :o, :r, :t, :t, :a, :k, :res, 'claude-sonnet-5-5', :usd, :settled, :at)"
                ),
                {
                    "id": a["id"],
                    "o": a["owner_id"],
                    "r": a["run_id"],
                    "t": a["task"],
                    "a": a.get("attempt", 1),
                    "k": a["kind"],
                    "res": a.get("reservation_id"),
                    "usd": Decimal(str(a.get("usd", 0))),
                    "settled": Decimal(str(a["settled_usd"])) if "settled_usd" in a else None,
                    "at": a.get("created_at", "2026-11-24T11:00:00Z"),  # "old" rows carry their own
                },
            )
        for q in seed["tutor_questions"]:
            await run(
                text(
                    "INSERT INTO tutor_questions (id, owner_id, lesson_id, revision_sha256, question, state, error_code,"
                    " created_at, finished_at) VALUES (:id, :o, :l, :rev, 'q', :s, :e, :c, :f)"
                ),
                {
                    "id": q["id"],
                    "o": q["owner_id"],
                    "l": "20000000-0000-4000-8000-000000000001",
                    "rev": "e" * 64,
                    "s": q["state"],
                    "e": q.get("error_code"),
                    "c": q["created_at"],
                    "f": q["finished_at"],
                },
            )
        for m in seed["materials"]:
            await run(
                text(
                    "INSERT INTO materials (owner_id, sha256, config_id, page_count) VALUES (:o, :s, 'm', :p)"
                ),
                {"o": m["owner_id"], "s": m["sha256"], "p": m["page_count"]},
            )
        for k in seed["job_keys"]:
            await run(
                text(
                    "INSERT INTO job_keys (kind, key, args, attempts, outcome, error_code, job_id, created_at, updated_at,"
                    " finished_at) VALUES (:kind, :key, CAST(:args AS jsonb), :att, :out, :code, :job, :c, :c, :f)"
                ),
                {
                    "kind": k["kind"],
                    "key": k["key"],
                    "args": json.dumps(k["args"]),
                    "att": k.get("attempts", 1),
                    "out": k["outcome"],
                    "code": k.get("error_code"),
                    "job": k["job_id"],
                    "c": k.get("created_at", "2026-11-24T10:00:00Z"),
                    "f": k.get("finished_at", "2026-11-24T11:00:00Z"),
                },
            )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_summary_on_vec_obs_3(pg: Any) -> None:
    from jobs.conftest import target
    from reader.db import make_engine, migrate

    summary = require("reader.obs.summary")
    engine = make_engine(target(pg))
    try:
        await migrate(engine)
        await _seed(engine)
        async with engine.connect() as conn:
            got = await summary.summary(conn, uuid.UUID(VEC["owner"]), days=7, now=NOW, tracing=TRACING)
            other = await summary.summary(conn, uuid.UUID(VEC["other_owner"]), days=7, now=NOW, tracing=TRACING)
    finally:
        await engine.dispose()
    expected = _strip(VEC["expected"])
    assert json.loads(json.dumps(got)) == expected
    # The other owner sees its own run and no row of the owner's; system rows are shown to any owner caller.
    assert [r["run_id"] for r in other["compile_runs"]] == ["10000000-0000-4000-8000-000000000009"]
    assert other["tutor"]["count"] == 0
    assert {(j["kind"], j["error_code"], j["system"]) for j in other["failures"]["jobs"]} == {
        ("pipeline.stage", "timeout", False),
        ("tutor.answer", "exhausted", False),  # its own dead answer job (round 1 seed)
        ("ingest.import", "ValueError", False),  # its own material's import
        ("ingest.import", "ValueError", True),  # the system import (no materials row)
        ("search.embed", "exhausted", True),
    }
    assert [(t["error_code"], t["count"]) for t in other["failures"]["tutor"]] == [("tutor_failed", 1)]
    assert other["ingest"]["rows"] == []
    assert '"question"' not in json.dumps(got)  # no text column is read (`question_id` is an id)


@pytest.mark.anyio
async def test_truncated_when_the_published_read_hits_its_limit(pg: Any) -> None:
    from jobs.conftest import target
    from reader.db import make_engine, migrate

    summary = require("reader.obs.summary")
    engine = make_engine(target(pg))
    try:
        await migrate(engine)
        await _seed(engine)
        async with engine.connect() as conn:
            got = await summary.summary(conn, uuid.UUID(VEC["owner"]), days=7, now=NOW, read_limit=2)
            one = await summary.summary(conn, uuid.UUID(VEC["owner"]), days=7, now=NOW, read_limit=1)
    finally:
        await engine.dispose()
    assert got["failures"]["truncated"] is True
    assert got["ingest"]["truncated"] is False  # one succeeded import: under a limit of 2
    assert one["ingest"]["truncated"] is True  # exactly the limit (round 1 survivor)


def test_limits_are_the_spec_numbers() -> None:
    """FR-OBS-10: newest 50 runs, slowest 10, up to 5 trace ids per group, newest 50 imports, reads of 500."""
    summary = require("reader.obs.summary")
    assert (summary.RUNS_LIMIT, summary.SLOWEST, summary.TRACE_IDS, summary.INGEST_LIMIT, summary.READ_LIMIT) == (
        50,
        10,
        5,
        50,
        500,
    )


@pytest.mark.anyio
async def test_the_limits_are_applied(pg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from jobs.conftest import target
    from reader.db import make_engine, migrate

    summary = require("reader.obs.summary")
    monkeypatch.setattr(summary, "SLOWEST", 2)
    monkeypatch.setattr(summary, "RUNS_LIMIT", 0)
    monkeypatch.setattr(summary, "INGEST_LIMIT", 0)
    engine = make_engine(target(pg))
    try:
        await migrate(engine)
        await _seed(engine)
        async with engine.connect() as conn:
            got = await summary.summary(conn, uuid.UUID(VEC["owner"]), days=7, now=NOW)
    finally:
        await engine.dispose()
    assert [s["ms"] for s in got["tutor"]["slowest"]] == [9000, 4000]
    assert got["compile_runs"] == [] and got["ingest"]["rows"] == []


@pytest.mark.parametrize(
    ("values", "p50", "p95"),
    [([1000] * 19 + [5000], 1000, 1000), ([1000] * 18 + [5000, 6000], 1000, 5000), (list(range(1, 101)), 50, 95)],
)
def test_p95_is_nearest_rank(values: list[int], p50: int, p95: int) -> None:
    summary = require("reader.obs.summary")
    assert (summary.nearest_rank(values, 50), summary.nearest_rank(values, 95)) == (p50, p95)


def _at(hours: float) -> datetime:
    return NOW + timedelta(hours=hours)


@pytest.mark.parametrize(
    ("moment", "created", "periods", "enabled", "traced"),
    [
        (_at(-1), None, [(_at(-48), True)], True, True),
        (_at(-1), None, [], True, False),  # before tracing ever started: the live check's skeleton links
        (_at(-50), None, [(_at(-48), True)], True, False),  # finished before the first traced worker start
        (_at(-1), None, [(_at(-48), True), (_at(-2), False)], True, False),  # tracing was off then
        (_at(-1), None, [(_at(-48), False), (_at(-2), True)], True, True),
        (_at(-2), None, [(_at(-2), True)], True, True),  # a start at the same instant counts
        (_at(-1), None, [(_at(-48), True)], False, False),  # tracing off in the api now
        (_at(-24 * 31), None, [(_at(-24 * 40), True)], True, False),  # older than retention_days (30)
        (None, _at(-1), [(_at(-48), True)], True, True),  # not finished: its creation counts
    ],
)
def test_trace_gate(
    moment: datetime | None,
    created: datetime | None,
    periods: list[tuple[datetime, bool]],
    enabled: bool,
    traced: bool,
) -> None:
    summary = require("reader.obs.summary")
    gate = summary.TraceGate(enabled, tuple(periods), NOW - timedelta(days=30))
    assert gate.traced(SimpleNamespace(finished_at=moment, created_at=created)) is traced


@pytest.mark.anyio
async def test_no_trace_ids_before_tracing_started(pg: Any) -> None:
    """Live check 2026-11-24: jobs that ran before tracing existed get no trace link (FR-OBS-10)."""
    from sqlalchemy import text

    from jobs.conftest import target
    from reader.db import make_engine, migrate

    summary = require("reader.obs.summary")
    engine = make_engine(target(pg))
    try:
        await migrate(engine)
        await _seed(engine)
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM obs_tracing"))
            # Tracing started after every seeded job finished (they finished by 11:31).
            await conn.execute(
                text("INSERT INTO obs_tracing (started_at, enabled) VALUES (:s, true)"), {"s": _at(-0.25)}
            )
        async with engine.connect() as conn:
            got = await summary.summary(conn, uuid.UUID(VEC["owner"]), days=7, now=NOW, tracing=TRACING)
            off = await summary.summary(
                conn, uuid.UUID(VEC["owner"]), days=7, now=NOW, tracing=SimpleNamespace(enabled=False, retention_days=30)
            )
    finally:
        await engine.dispose()
    for body in (got, off):
        text_ = json.dumps(body)
        assert '"trace_id": null' in text_
        assert all(not g["trace_ids"] for g in body["failures"]["jobs"] + body["failures"]["tutor"])
        assert all(s["trace_id"] is None for r in body["compile_runs"] for s in r["steps"])
        assert all(s["trace_id"] is None for s in body["tutor"]["slowest"])
        assert all(r["trace_id"] is None for r in body["ingest"]["rows"])
    assert [r["run_id"] for r in got["compile_runs"]] == ["10000000-0000-4000-8000-000000000001"]  # rows stay


@pytest.mark.anyio
async def test_the_worker_start_records_whether_tracing_is_on(pg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy import text

    from jobs.conftest import target
    from reader.db import make_engine, migrate
    from reader.obs import setup

    jobs = require("reader.obs.jobs")
    engine = make_engine(target(pg))
    enqueued: list[Any] = []

    class Queue:
        def __init__(self) -> None:
            self.engine = engine

        async def enqueue(self, kind: str, args: Any, schedule_in: int = 0) -> None:
            enqueued.append((kind, args))

    try:
        await migrate(engine)
        for enabled in (True, False):
            monkeypatch.setattr(setup, "current_settings", lambda e=enabled: setup.Settings(e, 30))
            await jobs.on_worker_start(Queue(), now=NOW)
        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT enabled FROM obs_tracing ORDER BY started_at"))).scalars().all()
    finally:
        await engine.dispose()
    assert rows == [True, False]
    assert enqueued == [("obs.retention", {"day": "2026-11-24"})] * 2
