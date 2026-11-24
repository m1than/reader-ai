"""SRV-OBS [unit], phase 2, code review round 1 fixes (ADR-0038): `request` refuses an undeclared MLflow request
before any HTTP request; the label mirror's 404 rule (FR-OBS-24 c with FR-OBS-14: transient, then no trace,
never dead); the bench's duplicate and unregistered ids and the tutor Run's params (FR-OBS-23); the freshness
daily cap, tracing-off stale health and surfaced database errors in alerts (FR-OBS-25); and INV-OBS-2 as a
property over arbitrary snapshots and uploads: only VEC-OBS-5-valid input yields a row, a job or a request, and
no canary reaches any body.

@trace FR-OBS-23 FR-OBS-24 FR-OBS-25 VEC-OBS-5 INV-OBS-2
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from .conftest import FIXTURES, REPO, load_yaml, require

VEC5 = load_yaml(FIXTURES / "phase2.yaml")
CANARIES = ("bookcanary-7f3a9c", "questioncanary-1b2c", "answercanary-9d8e", "prosecanary-3c4d")
LESSON = "20000000-0000-4000-8000-0000000000bb"
TRACE = "tr-" + "b" * 32
V1 = (REPO / "spikes/embeddings-local/questions.yaml").read_text(encoding="utf-8")
TUTOR_SET = (FIXTURES / "bench-tutor-v2.yaml").read_text(encoding="utf-8")


class Mlflow:
    """An in-test MLflow: answers per (method, path) from `replies`, records every request."""

    def __init__(self, replies: dict[tuple[str, str], Any] | None = None) -> None:
        self.replies, self.requests = dict(replies or {}), []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.get((request.method, request.url.path), (200, {}))
        if callable(reply):
            reply = reply(request)
        status, body = reply if isinstance(reply, tuple) else (200, reply)
        return httpx.Response(status, json=body)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def text(self) -> str:
        return "".join((r.content or b"").decode() + str(r.url) for r in self.requests)


def _search(**over: Any) -> dict[str, Any]:
    ids = [q["id"] for q in load_yaml(REPO / "spikes/embeddings-local/questions.yaml")["questions"]]
    keep = [i for i in ids if i not in VEC5["bench_sets"]["search-bench-v1"]["exclude"]]
    per = [{"id": i, "rank": 1 + n % 4, "hit": True, "top_pages": [1]} for n, i in enumerate(keep)]
    base = {"kind": "search", "model": "BAAI/bge-m3", "chunk_chars": 1200, "overlap": 200,
            "questions": len(per), "per_question": per}
    base.update(over)
    return base


def _tutor(**over: Any) -> dict[str, Any]:
    ids = [q["id"] for q in load_yaml(FIXTURES / "bench-tutor-v2.yaml")["questions"]]
    records = [{"id": i, "chapter": 1, "question_id": None, "state": "answered", "wall_s": 3.0, "usd": 0.01,
                "output_tokens": 10, "thinking_tokens": None, "grounded": True, "insufficient": False,
                "blocks": 1, "blocks_with_verified_quote": 1, "gold_in_top8": True, "gold_cited": True}
               for i in ids]
    base = {"kind": "tutor", "date": "2026-11-23", "branch": "V/slice-integration", "commit": "bba0a99",
            "lesson_id": LESSON, "set": "tutor-bench-v2", "records": records}
    base.update(over)
    return base


# --- `request`: an undeclared request is refused before any HTTP request ---


@pytest.mark.parametrize(("method", "path"), [
    ("DELETE", "/api/3.0/mlflow/traces/get"),          # a declared path with an undeclared method
    ("POST", "/api/2.0/mlflow/experiments/delete"),    # an undeclared path
    ("GET", "/api/2.0/mlflow/experiments/get/../../x"),  # a declared prefix with more segments
    ("POST", f"/api/3.0/mlflow/traces/{TRACE}/assessments/extra"),
])
@pytest.mark.anyio
async def test_request_refuses_an_undeclared_request_before_sending(method: str, path: str) -> None:
    phase2 = require("reader.obs.phase2")
    mlflow = Mlflow()
    async with mlflow.client() as c:
        with pytest.raises(ValueError, match="undeclared"):
            await phase2.request(c, method, path, {"x": 1})
    assert mlflow.requests == []


# --- FR-OBS-24 (c): a missing trace (404) is transient, then a no-trace end, never dead ---


def _label(revision: int = 1, assessment_id: str | None = None) -> Any:
    labels = require("reader.obs.labels")
    return labels.Label(target="lesson", target_id=LESSON, ok=True, reason=None, revision=revision,
                        assessment_id=assessment_id)


@pytest.mark.anyio
@pytest.mark.parametrize("missing_at", ["GET", "POST"])
async def test_a_missing_trace_is_transient_then_ends_without_a_post(missing_at: str, caplog: Any) -> None:
    labels = require("reader.obs.labels")
    get = ("GET", "/ajax-api/3.0/mlflow/traces/get")
    post = ("POST", f"/api/3.0/mlflow/traces/{TRACE}/assessments")
    replies: dict[tuple[str, str], Any] = {get: {"trace": {"trace_info": {"assessments": []}}}}
    replies[get if missing_at == "GET" else post] = (404, {"error_code": "RESOURCE_DOES_NOT_EXIST"})
    mlflow = Mlflow(replies)
    async with mlflow.client() as c:
        with pytest.raises(labels.TraceMissing):  # attempts remain: retried
            await labels.mirror(_label(), job_revision=1, trace_ref=TRACE, client=c)
        with caplog.at_level(logging.INFO, logger="reader.obs"):
            assert await labels.mirror(_label(), job_revision=1, trace_ref=TRACE, client=c, last=True) is None
    assert "obs label no_trace target=lesson" in caplog.text
    posts = [r for r in mlflow.requests if r.method == "POST"]
    assert len(posts) == (0 if missing_at == "GET" else 2)
    assert labels.TraceMissing in labels.KIND_DECL.transient
    assert labels.KIND_DECL.max_attempts == labels.MAX_ATTEMPTS == 3


@pytest.mark.anyio
async def test_another_4xx_is_refused_not_transient() -> None:
    labels, phase2 = require("reader.obs.labels"), require("reader.obs.phase2")
    mlflow = Mlflow({("GET", "/ajax-api/3.0/mlflow/traces/get"): (400, {})})
    async with mlflow.client() as c:
        with pytest.raises(phase2.MlflowRefused):
            await labels.mirror(_label(), job_revision=1, trace_ref=TRACE, client=c, last=True)
    assert phase2.MlflowRefused not in labels.KIND_DECL.transient


# --- FR-OBS-23: duplicate and unregistered ids; the tutor Run's params ---


@pytest.mark.parametrize("change", ["duplicate", "unregistered", "empty"])
def test_duplicate_unregistered_or_no_ids_are_bench_invalid(change: str) -> None:
    bench = require("reader.obs.bench")
    for set_text, result, rows in ((V1, _search(), "per_question"), (TUTOR_SET, _tutor(), "records")):
        bad = json.loads(json.dumps(result))
        if change == "duplicate":
            bad[rows][1]["id"] = bad[rows][0]["id"]
        elif change == "unregistered":
            bad[rows][0]["id"] = "z99"
        else:
            bad[rows] = []
        with pytest.raises(bench.BenchError) as e:
            bench.validate(set_text, bad)
        assert e.value.code == "bench_invalid", (set_text[:20], change)


@pytest.mark.anyio
async def test_a_bench_write_of_a_complete_run_makes_no_run_write() -> None:
    bench = require("reader.obs.bench")
    valid = bench.validate(TUTOR_SET, _tutor())
    row = {"kind": valid.kind, "set_name": valid.set_name, "set_sha256": valid.set_sha256, "model": None,
           "commit": valid.commit, "params": valid.params, "scores": valid.scores, "per_question": [],
           "records": valid.records, "created_at": datetime(2026, 11, 25, tzinfo=UTC)}
    complete = {"info": {"run_id": "r-1"}, "data": {"tags": [{"key": "reader.complete", "value": "true"}]}}
    mlflow = Mlflow({
        ("GET", "/api/2.0/mlflow/experiments/get-by-name"): {"experiment": {"experiment_id": "1"}},
        ("POST", "/api/3.0/mlflow/datasets/search"): {"datasets": [{"dataset_id": "d-1"}]},
        ("POST", "/api/2.0/mlflow/runs/search"): {"runs": [complete]},
    })
    async with mlflow.client() as c:
        assert await bench.write(c, "u-1", row) == "r-1"
    run_writes = [r.url.path for r in mlflow.requests if "/runs/" in r.url.path and not r.url.path.endswith("search")]
    assert run_writes == []


def test_the_tutor_run_logs_only_the_params_that_apply() -> None:
    bench = require("reader.obs.bench")
    upload = bench.validate(TUTOR_SET, _tutor())
    assert upload.kind == "tutor" and upload.model is None and upload.commit == "bba0a99"
    assert upload.params == {"lesson_id": LESSON, "date": "2026-11-23"}  # no model, chunk_chars or overlap
    search = bench.validate(V1, _search())
    assert search.params == {"model": "BAAI/bge-m3", "chunk_chars": 1200, "overlap": 200}


# --- FR-OBS-25: the freshness cap, stale health while tracing is off, surfaced errors ---


def test_the_freshness_tasks_count_against_one_daily_cap() -> None:
    alerts = require("reader.obs.alerts")
    from reader.freshness import params as freshness

    caps = alerts.daily_caps()
    assert caps["freshness"] == float(freshness.DAILY_CAP_USD) and "tutor" in caps
    assert {alerts.subject(t) for t in freshness.TASKS} == {"freshness"}
    assert alerts.subject("tutor") == "tutor"


@pytest.mark.parametrize(("tracing", "age_min", "alerts_n"), [
    (False, 300, 0),  # tracing off, an old row: stale by design, no alert
    (True, 76, 1),    # tracing on, older than 75 minutes: obs_unhealthy
    (True, 75, 0),    # the boundary is not stale
    (True, 5, 0),     # a fresh row: no alert
])
def test_obs_unhealthy_uses_healths_one_staleness_rule(tracing: bool, age_min: int, alerts_n: int,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-OBS-25 with FR-OBS-32: the alert asks `health.stale(checked_at, now, tracing_on)`."""
    alerts = require("reader.obs.alerts")
    health = require("reader.obs.health")
    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    asked: list[tuple[datetime, datetime, bool]] = []
    real = health.stale

    def spy(checked_at: datetime, at: datetime, tracing_on: bool) -> bool:
        asked.append((checked_at, at, tracing_on))
        return bool(real(checked_at, at, tracing_on))

    monkeypatch.setattr(health, "stale", spy)
    checked = now - timedelta(minutes=age_min)
    got = alerts.compute(task_spend_today={}, task_daily_caps={}, open_runs=[], dead_jobs={}, bench=[],
                         stack={"mlflow": (True, checked)}, now=now, tracing=tracing)
    assert [(a["code"], a["subject"]) for a in got] == [("obs_unhealthy", "mlflow")] * alerts_n
    assert asked == ([(checked, now, True)] if tracing else [])


@pytest.mark.anyio
async def test_load_sums_the_freshness_tasks_and_skips_stale_health_while_tracing_is_off() -> None:
    alerts = require("reader.obs.alerts")
    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    stale = now - timedelta(hours=5)
    replies = {
        "agent_spend s WHERE owner_id": [("freshness_scan", 0.9), ("freshness_report", 0.8), ("tutor", 0.1)],
        "obs_stack_status": [("mlflow", True, "healthy", stale), ("rotel", True, "listening", stale)],
    }
    conn = _FakeConn(replies)
    off = await alerts.load(conn, uuid.uuid4(), {}, now, tracing=False)
    assert [(a["code"], a["subject"]) for a in off] == [("daily_spend_high", "freshness")]  # 1.7 >= 0.8 of 2.00
    on = await alerts.load(conn, uuid.uuid4(), {}, now, tracing=True)
    assert sorted(a["subject"] for a in on if a["code"] == "obs_unhealthy") == ["mlflow", "rotel"]


@pytest.mark.anyio
async def test_a_database_error_surfaces_and_only_a_missing_table_reads_as_no_rows() -> None:
    from sqlalchemy.exc import OperationalError, ProgrammingError

    alerts = require("reader.obs.alerts")

    class Orig(Exception):
        def __init__(self, sqlstate: str) -> None:
            super().__init__(sqlstate)
            self.sqlstate = sqlstate

    missing = _FakeConn({}, fail=ProgrammingError("SELECT", {}, Orig("42P01")))
    assert await alerts._rows(missing, alerts.STACK_SQL, {}) == []
    for failure in (ProgrammingError("SELECT", {}, Orig("42703")), OperationalError("SELECT", {}, Orig("08006"))):
        with pytest.raises(type(failure)):
            await alerts._rows(_FakeConn({}, fail=failure), alerts.STACK_SQL, {})


class _FakeConn:
    """Answers `execute` by the first `replies` key found in the SQL; `fail` raises from every execute."""

    def __init__(self, replies: dict[str, list[tuple[Any, ...]]], fail: Exception | None = None) -> None:
        self.replies, self.fail = replies, fail

    @asynccontextmanager
    async def begin_nested(self) -> Any:
        yield self

    async def execute(self, sql: Any, params: Any = None) -> Any:
        if self.fail is not None:
            raise self.fail
        rows = next((v for k, v in self.replies.items() if k in str(sql)), [])

        class Result:
            def all(self) -> list[tuple[Any, ...]]:
                return list(rows)

        return Result()


# --- INV-OBS-2 (phase 2) as a property: only valid input yields a row, a job or a request; no canary ---

# Book text, questions and answers are prose: a canary always rides in prose here. A bare token-shaped canary
# would pass a token-shaped rule (reader_version, hf_model) by design; those values come from code, not content.
PROSE = st.sampled_from(CANARIES).flatmap(
    lambda c: st.sampled_from([f"Глава 1: {c} текст", f"{c} and more", f"x/{c} y", f"{c}\n", f"«{c}»"]))
VALUES = st.one_of(PROSE, st.integers(-5, 10**8), st.floats(allow_nan=False), st.booleans(), st.none(),
                   st.lists(PROSE, max_size=2), st.dictionaries(st.sampled_from(["note", "text"]), PROSE, max_size=1))


@st.composite
def uploads(draw: Any) -> tuple[str, Any]:
    """A valid upload of either kind with up to three edits: a key set to an arbitrary value, a key added, a
    per-question field replaced, or the set text changed."""
    tutor = draw(st.booleans())
    set_text, result = (TUTOR_SET, _tutor()) if tutor else (V1, _search())
    rows = "records" if tutor else "per_question"
    for _ in range(draw(st.integers(0, 3))):
        edit = draw(st.sampled_from(["set_key", "add_key", "row_field", "set_text"]))
        if edit == "set_key":
            result[draw(st.sampled_from(sorted(result)))] = draw(VALUES)
        elif edit == "add_key":
            result[draw(st.sampled_from(["note", "summary", "question", "answer"]))] = draw(PROSE)
        elif edit == "row_field" and isinstance(result.get(rows), list) and result[rows]:
            row = result[rows][draw(st.integers(0, len(result[rows]) - 1))]
            row[draw(st.sampled_from([*sorted(row), "text"]))] = draw(VALUES)
        else:
            set_text = set_text + "\n# " + draw(PROSE)
    return set_text, result


class _RecordingConn:
    def __init__(self) -> None:
        self.statements: list[tuple[str, Any]] = []

    async def execute(self, sql: Any, params: Any = None) -> Any:
        self.statements.append((str(sql), params))

        class Result:
            def first(self) -> Any:
                return (uuid.uuid4(),)

        return Result()


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(uploads())
def test_only_valid_uploads_yield_a_row_a_job_or_a_request_and_no_canary_reaches_any(upload: Any) -> None:
    bench = require("reader.obs.bench")
    set_text, result = upload
    conn, mlflow = _RecordingConn(), Mlflow({
        ("GET", "/api/2.0/mlflow/experiments/get-by-name"): {"experiment": {"experiment_id": "1"}},
        ("POST", "/api/3.0/mlflow/datasets/search"): {"datasets": [{"dataset_id": "d-1", "digest": "abcd"}]},
        ("POST", "/api/2.0/mlflow/runs/search"): {"runs": []},
        ("POST", "/api/2.0/mlflow/runs/create"): {"run": {"info": {"run_id": "r-1"}}},
    })
    try:
        valid = bench.validate(set_text, result)
    except bench.BenchError:
        return  # refused at the route: no row, no job, no request
    owner = uuid.uuid4()

    async def run() -> None:
        upload_id = await bench.record(conn, owner, valid)
        row = {"kind": valid.kind, "set_name": valid.set_name, "set_sha256": valid.set_sha256,
               "model": valid.model, "commit": valid.commit, "params": valid.params, "scores": valid.scores,
               "per_question": valid.per_question, "records": valid.records,
               "created_at": datetime(2026, 11, 25, tzinfo=UTC)}
        async with mlflow.client() as c:
            await bench.write(c, str(upload_id), row)

    asyncio.run(run())
    stored = json.dumps([p for _, p in conn.statements], default=str)
    assert [s.split()[2] for s, _ in conn.statements] == ["obs_bench_results", "job_keys"]  # one row, one job
    assert not any(c in stored or c in mlflow.text() for c in CANARIES)


@settings(max_examples=150, deadline=None)
@given(
    tags=st.dictionaries(st.sampled_from(["reader.lesson.id", "reader.course.id", "reader.note"]), PROSE,
                         max_size=2),
    params=st.dictionaries(st.sampled_from(["version_key", "model.draft", "reader_version", "prompt"]), PROSE,
                           max_size=2),
)
def test_no_snapshot_carries_a_canary_into_a_run(tags: dict[str, str], params: dict[str, str]) -> None:
    runs = require("reader.obs.runs")
    base_tags = {"reader.lesson.id": LESSON, "reader.course.id": "50000000-0000-4000-8000-0000000000cc"}
    base_params = {"version_key": "abababababab", "reader_version": "0.1.0", "model.draft": "claude-sonnet-5-5"}
    snapshot = runs.Snapshot(kind="compile", key="10000000-0000-4000-8000-0000000000aa", name="compile 10000000",
                             start_ms=1_795_000_000_000, end_ms=1_795_000_160_000, status="FINISHED",
                             outcome="finished", end=1, tags={**base_tags, **tags},
                             params={**base_params, **params}, metrics={"cost_usd.total": 1.0},
                             snapshot_at="2026-11-25T12:00:00Z")
    mlflow = Mlflow({("POST", "/api/2.0/mlflow/runs/search"): {"runs": []},
                     ("POST", "/api/2.0/mlflow/runs/create"): {"run": {"info": {"run_id": "r1"}, "data": {}}},
                     ("POST", "/ajax-api/3.0/mlflow/traces/search"): {"traces": []}})

    async def run() -> None:
        async with mlflow.client() as c:
            await runs.sync(snapshot, c)

    try:
        asyncio.run(run())
    except ValueError:
        writes = [r for r in mlflow.requests if r.method != "GET" and not r.url.path.endswith("/search")]
        assert writes == []  # refused before any write
        return
    assert not any(c in mlflow.text() for c in CANARIES)
