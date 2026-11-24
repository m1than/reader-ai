"""SRV-OBS [unit], phase 2 code review round 2 (ADR-0038): VEC-OBS-5's rule forms as a table (`phase2.check`);
a Run refuses an unknown param, metric or tag key and an undeclared `<stage>` suffix before any request, and its
`reader.ends` never moves backwards (FR-OBS-20); every required bench key is required (FR-OBS-23); the label
mirror finds an existing assessment by both its name and its revision (FR-OBS-24 c); the alerts' mirror of the
freshness task names and daily cap equals SRV-FRESH's (FR-OBS-25, INV-FRESH-5).

@trace FR-OBS-20 FR-OBS-23 FR-OBS-24 FR-OBS-25 VEC-OBS-5
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from .conftest import require
from .test_phase2_review import TUTOR_SET, V1, _search, _tutor
from .test_phase2_unit import RUN, Mlflow, _search_reply, _snapshot

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.parametrize(("value", "rule", "ok"), [
    ("finished", {"enum": ["finished", "failed"]}, True),
    ("other", {"enum": ["finished", "failed"]}, False),
    (1, {"int": [1, 100]}, True),
    (100, {"int": [1, 100]}, True),
    (0, {"int": [1, 100]}, False),
    (101, {"int": [1, 100]}, False),
    (True, {"int": [0, 1]}, False),  # a bool is never an int
    (1.5, {"int": [0, 10]}, False),
    (0.5, {"float": [0, 1]}, True),
    (2, {"float": [0, 1]}, False),
    ("abababababab", "version_key", True),  # a named pattern
    ("abababababab0", "version_key", False),  # anchored at the end
    ("xabababababab", "version_key", False),  # anchored at the start
    ("ab12", "^[a-z0-9]{4}$", True),  # a literal regex
    ("ab12\n", "^[a-z0-9]{4}$", False),  # fullmatch: no trailing newline
    (12, "^[0-9]{2}$", False),  # a pattern only matches a string
    ("draft", "stage", True),
    ("not_a_stage", "stage", False),
    ("x", None, False),  # no rule: refused
    ("x", {"unknown": 1}, False),
])
def test_check_rule_forms(value: Any, rule: Any, ok: bool) -> None:
    assert require("reader.obs.phase2").check(value, rule) is ok


@pytest.mark.parametrize("over", [
    {"metrics": {"cost_usd.total": 1.0, "secret_metric": 1}},
    {"metrics": {"cost_usd.not_a_stage": 1.0}},
    {"params": {"version_key": "abababababab", "model.not_a_stage": "claude-sonnet-5-5"}},
    {"params": {"free_text": "x"}},
    {"tags": {"reader.lesson.id": "20000000-0000-4000-8000-0000000000bb", "reader.note": "x"}},
])
async def test_a_run_refuses_an_unknown_key_before_any_request(over: dict[str, Any]) -> None:
    runs = require("reader.obs.runs")
    mlflow = Mlflow()
    async with mlflow.client() as c:
        with pytest.raises(ValueError):
            await runs.sync(_snapshot(**over), c)
    assert mlflow.requests == []


async def test_reader_ends_never_moves_backwards() -> None:
    """A late older end (end 2 after the Run already says 3) keeps the newer end's tags, status and end time."""
    runs = require("reader.obs.runs")
    found = {"info": {"run_id": "r1"}, "data": {"params": [], "tags": [{"key": "reader.ends", "value": "3"}]}}
    mlflow = Mlflow({("POST", "/api/2.0/mlflow/runs/search"): _search_reply([found])})
    async with mlflow.client() as c:
        await runs.sync(_snapshot(end=2, status="FAILED", outcome="failed"), c)
        await runs.sync(_snapshot(end=4), c)
    bodies = [(r.url.path, json.loads(r.content)) for r in mlflow.requests if r.content]
    batches = [b for p, b in bodies if p.endswith("/runs/log-batch")]
    tags = [{t["key"]: t["value"] for t in b["tags"]} for b in batches]
    assert tags[0] == {"reader.ends": "3"}  # no outcome, snapshot or lesson tags from the late end
    assert {m["step"] for m in batches[0]["metrics"]} == {2}  # its metrics still land at its own step
    assert tags[1]["reader.ends"] == "4" and tags[1]["reader.outcome"] == "finished"
    updates = [b for p, b in bodies if p.endswith("/runs/update")]
    assert [u["status"] for u in updates] == ["FINISHED"]  # only the newer end updates the Run
    completes = [b for p, b in bodies if p.endswith("/runs/set-tag")]
    assert len(completes) == 2  # both attempts complete the Run


@pytest.mark.parametrize(("kind", "key"), [
    *[("search", k) for k in ("kind", "model", "chunk_chars", "overlap", "questions", "per_question")],
    *[("tutor", k) for k in ("kind", "date", "branch", "commit", "lesson_id", "set", "records")],
])
def test_every_required_bench_key_is_required(kind: str, key: str) -> None:
    bench = require("reader.obs.bench")
    set_text, result = (V1, _search()) if kind == "search" else (TUTOR_SET, _tutor())
    bench.validate(set_text, result)  # the full result is valid
    del result[key]
    with pytest.raises(bench.BenchError) as refused:
        bench.validate(set_text, result)
    assert refused.value.code == "bench_invalid"


@pytest.mark.parametrize(("there", "posts"), [
    ({"assessment_name": "owner.lesson_ok", "metadata": {"revision": "2"}, "assessment_id": "a-2"}, False),
    ({"assessment_name": "owner.lesson_ok", "metadata": {"revision": "1"}, "assessment_id": "a-1"}, True),
    ({"assessment_name": "owner.answer_ok", "metadata": {"revision": "2"}, "assessment_id": "a-x"}, True),
    ({"assessment_name": "owner.lesson_ok", "metadata": {}, "assessment_id": "a-n"}, True),
])
async def test_the_mirror_reuses_an_assessment_only_by_name_and_revision(there: dict[str, Any],
                                                                         posts: bool) -> None:
    labels = require("reader.obs.labels")
    label = labels.Label(target="lesson", target_id=RUN, ok=True, reason=None, revision=2, assessment_id="a-1")
    trace = "tr-" + "c" * 32
    mlflow = Mlflow({
        ("GET", "/ajax-api/3.0/mlflow/traces/get"): {"trace": {"trace_info": {"assessments": [there]}}},
        ("POST", f"/api/3.0/mlflow/traces/{trace}/assessments"): {"assessment": {"assessment_id": "a-new"}},
    })
    async with mlflow.client() as c:
        got = await labels.mirror(label, job_revision=2, trace_ref=trace, client=c)
    assert got == ("a-new" if posts else "a-2")
    posted = [json.loads(r.content) for r in mlflow.requests if r.method == "POST"]
    assert len(posted) == int(posts)
    if posts:
        assert posted[0]["assessment"]["overrides"] == "a-1"
        assert posted[0]["assessment"]["metadata"]["revision"] == "2"


def test_the_alerts_freshness_mirror_equals_srv_fresh() -> None:
    """INV-FRESH-5 quarantines reader.freshness from src; a test may import it to pin the mirror."""
    from reader.freshness import params as freshness

    alerts = require("reader.obs.alerts")
    assert alerts.FRESHNESS_TASKS == freshness.TASKS
    assert alerts.FRESHNESS_DAILY_CAP_USD == freshness.DAILY_CAP_USD
    assert {alerts.subject(t) for t in freshness.TASKS} == {"freshness"}
    assert alerts.daily_caps()["freshness"] == float(freshness.DAILY_CAP_USD)
