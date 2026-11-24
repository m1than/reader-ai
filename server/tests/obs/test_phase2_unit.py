"""SRV-OBS [unit], phase 2 (ADR-0038): the worker's MLflow writes as REST against an in-test MLflow (httpx
MockTransport recording every request): Runs (FR-OBS-20), versions (FR-OBS-21), the bench upload's validation and
recomputed scores (FR-OBS-22, -23), label mirrors (FR-OBS-24), alerts (FR-OBS-25), the declarative setup
(FR-OBS-26), retention of Runs and LoggedModels (FR-OBS-31), the health probe (FR-OBS-32); the VEC-OBS-5/6
catalogues; INV-OBS-2's phase 2 canary over every body and INV-OBS-3's phase 2 isolation.

@trace FR-OBS-20 FR-OBS-21 FR-OBS-22 FR-OBS-23 FR-OBS-24 FR-OBS-25 FR-OBS-26 FR-OBS-31 FR-OBS-32 VEC-OBS-5 VEC-OBS-6 INV-OBS-2 INV-OBS-3
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from .conftest import FIXTURES, REPO, load_yaml, require

VEC5 = load_yaml(FIXTURES / "phase2.yaml")
VEC6 = load_yaml(FIXTURES / "mlflow-setup.yaml")
CANARIES = ("bookcanary-7f3a9c", "questioncanary-1b2c", "answercanary-9d8e", "prosecanary-3c4d")
RUN = "10000000-0000-4000-8000-0000000000aa"
LESSON = "20000000-0000-4000-8000-0000000000bb"

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Mlflow:
    """An in-test MLflow: answers per (method, path) from `replies` (a callable or a body), records requests."""

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

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]

    def bodies(self) -> list[Any]:
        return [json.loads(r.content) if r.content else None for r in self.requests]

    def writes(self) -> list[str]:
        reads = ("/search", "/get", "/get-by-name")
        return [p for p in self.paths() if p.startswith(("POST", "PATCH", "DELETE")) and not p.endswith(reads)]


# --- VEC-OBS-5 / VEC-OBS-6: the module catalogues equal the fixtures ---


def test_catalogues_equal_vec_obs_5_and_6() -> None:
    phase2 = require("reader.obs.phase2")
    assert phase2.VEC == VEC5
    assert phase2.SETUP == VEC6
    for pattern in VEC5["patterns"].values():
        if isinstance(pattern, str):
            re.compile(pattern)
    assert VEC5["patterns"]["commit"] == "^[0-9a-f]{7,40}$"
    assert [e["name"] for e in VEC6["experiments"][1:]] == ["bench"]
    assert VEC6["experiments"][0]["id"] == "0" and VEC6["experiments"][0]["name"] == "reader"


def test_writes_are_the_declared_list_only() -> None:
    phase2 = require("reader.obs.phase2")
    declared = {(w["method"], w["path"]) for w in VEC5["writes"]}
    assert set(phase2.WRITE_PATHS) == declared  # every request a phase 2 job may make


# --- FR-OBS-20: Runs ---


def _snapshot(**over: Any) -> Any:
    runs = require("reader.obs.runs")
    base = dict(kind="compile", key=RUN, name=f"compile {RUN[:8]}", start_ms=1_795_000_000_000,
                end_ms=1_795_000_160_000, status="FINISHED", outcome="finished", end=1,
                tags={"reader.lesson.id": LESSON, "reader.course.id": "50000000-0000-4000-8000-0000000000cc"},
                params={"version_key": "abababababab", "reader_version": "0.1.0",
                        "model.draft": "claude-sonnet-5-5"},
                metrics={"cost_usd.total": 1.84, "cost_usd.draft": 0.62, "claims.supported": 13},
                snapshot_at="2026-11-25T12:00:00Z")
    base.update(over)
    return runs.Snapshot(**base)


def _search_reply(found: list[dict[str, Any]]) -> Any:
    return lambda req: (200, {"runs": found})


def _traces_reply(n: int) -> Any:
    ids = [f"tr-{i:032x}" for i in range(n)]

    def reply(req: httpx.Request) -> tuple[int, dict[str, Any]]:
        body = json.loads(req.content)
        start = int(body.get("page_token") or 0)
        page = ids[start:start + 50]
        token = str(start + 50) if start + 50 < n else None
        return 200, {"traces": [{"trace_id": t} for t in page], **({"next_page_token": token} if token else {})}

    return reply


async def test_a_new_run_is_created_logged_linked_and_completed_last() -> None:
    runs = require("reader.obs.runs")
    mlflow = Mlflow({
        ("POST", "/api/2.0/mlflow/runs/search"): _search_reply([]),
        ("POST", "/api/2.0/mlflow/runs/create"): {"run": {"info": {"run_id": "r1"}, "data": {}}},
        ("POST", "/ajax-api/3.0/mlflow/traces/search"): _traces_reply(120),
    })
    async with mlflow.client() as c:
        await runs.sync(_snapshot(), c)
    paths = mlflow.paths()
    assert paths[:2] == ["POST /api/2.0/mlflow/runs/search", "POST /api/2.0/mlflow/runs/create"]
    links = [b for p, b in zip(paths, mlflow.bodies(), strict=True) if p.endswith("/traces/link-to-run")]
    assert [len(b["trace_ids"]) for b in links] == [100, 20]  # at most 100 a call (round 8)
    assert paths[-2:] == ["POST /api/2.0/mlflow/runs/update", "POST /api/2.0/mlflow/runs/set-tag"]
    assert mlflow.bodies()[-1] == {"run_id": "r1", "key": "reader.complete", "value": "true"}  # LAST
    search = mlflow.bodies()[0]
    assert search["experiment_ids"] == ["0"] and search["max_results"] == 2
    assert f"tags.`reader.run_id` = '{RUN}'" in search["filter"]
    batch = next(b for p, b in zip(paths, mlflow.bodies(), strict=True) if p.endswith("/runs/log-batch"))
    assert {p["key"] for p in batch["params"]} == {"version_key", "reader_version", "model.draft"}
    assert {m["key"]: m["step"] for m in batch["metrics"]} == {"cost_usd.total": 1, "cost_usd.draft": 1,
                                                               "claims.supported": 1}
    tags = {t["key"]: t["value"] for t in batch["tags"]}
    assert tags["reader.snapshot_at"] == "2026-11-25T12:00:00Z" and tags["reader.ends"] == "1"
    assert "snapshot_at" not in {p["key"] for p in batch["params"]}  # a tag, not a param (round 2, N1)


async def test_an_incomplete_run_logs_only_the_params_it_lacks() -> None:
    runs = require("reader.obs.runs")
    found = [{"info": {"run_id": "r1"}, "data": {"params": [{"key": "version_key", "value": "abababababab"}],
                                                  "tags": [{"key": "reader.run_id", "value": RUN}]}}]
    mlflow = Mlflow({("POST", "/api/2.0/mlflow/runs/search"): _search_reply(found),
                     ("POST", "/ajax-api/3.0/mlflow/traces/search"): _traces_reply(0)})
    async with mlflow.client() as c:
        await runs.sync(_snapshot(end=2), c)
    assert "POST /api/2.0/mlflow/runs/create" not in mlflow.paths()
    batch = next(b for p, b in zip(mlflow.paths(), mlflow.bodies(), strict=True) if p.endswith("/runs/log-batch"))
    assert {p["key"] for p in batch["params"]} == {"reader_version", "model.draft"}  # version_key is there
    assert {m["step"] for m in batch["metrics"]} == {2}


async def test_two_runs_for_one_id_are_a_duplicate() -> None:
    runs = require("reader.obs.runs")
    found = [{"info": {"run_id": "a"}, "data": {}}, {"info": {"run_id": "b"}, "data": {}}]
    mlflow = Mlflow({("POST", "/api/2.0/mlflow/runs/search"): _search_reply(found)})
    async with mlflow.client() as c:
        with pytest.raises(runs.ObsRunsDuplicate):
            await runs.sync(_snapshot(), c)
    assert mlflow.writes() == []


async def test_an_import_finds_its_traces_by_the_material_tag() -> None:
    runs = require("reader.obs.runs")
    mat = "aa11" * 16
    mlflow = Mlflow({("POST", "/api/2.0/mlflow/runs/search"): _search_reply([]),
                     ("POST", "/api/2.0/mlflow/runs/create"): {"run": {"info": {"run_id": "r2"}, "data": {}}},
                     ("POST", "/ajax-api/3.0/mlflow/traces/search"): _traces_reply(3)})
    snap = _snapshot(kind="ingest", key="40000000-0000-4000-8000-000000000001", name=f"import {mat[:8]}",
                     trace_tag=("reader.material_sha256", mat), params={"converter_version": "r2",
                                                                        "material_sha256": mat},
                     metrics={"pages": 594, "chunks": 2285}, tags={})
    async with mlflow.client() as c:
        await runs.sync(snap, c)
    search = next(b for p, b in zip(mlflow.paths(), mlflow.bodies(), strict=True) if p.endswith("traces/search"))
    assert search["filter"] == f"tag.`reader.material_sha256` = '{mat}'" and search["max_results"] == 500


def test_runs_kind_declaration() -> None:
    runs = require("reader.obs.runs")
    k = runs.KIND_DECL
    assert (k.name, k.queue, k.key, k.max_attempts, k.timeout_s) == ("obs.runs", "obs", ("kind", "id", "end"), 3, 60)
    assert runs.SCHEDULE_IN_S == 60 and runs.LINK_BATCH == 100


# --- FR-OBS-21, FR-OBS-26: the version key and the declarative setup ---


def test_the_version_key_is_the_sha256_of_its_canonical_inputs() -> None:
    boot = require("reader.obs.bootstrap")
    inputs = {"prompt_set_sha256": "ab" * 32, "stage_models": {"tutor": "claude-sonnet-5-5"},
              "pipeline_config_sha256": "cd" * 32}
    want = hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert boot.version_key(**inputs) == want
    assert re.fullmatch(VEC5["patterns"]["version_key"], boot.short_key(want))


def _experiments(state: dict[str, Any]) -> dict[tuple[str, str], Any]:
    def get(req: httpx.Request) -> tuple[int, dict[str, Any]]:
        exp = state["exp0"]
        return 200, {"experiment": {"experiment_id": "0", "name": exp["name"],
                                    "tags": [{"key": k, "value": v} for k, v in exp["tags"].items()]}}

    def by_name(req: httpx.Request) -> tuple[int, dict[str, Any]]:
        return (200, {"experiment": {"experiment_id": "1", "name": "bench", "tags": []}}) if state["bench"] else (404, {})

    def update(req: httpx.Request) -> tuple[int, dict[str, Any]]:
        state["exp0"]["name"] = json.loads(req.content)["new_name"]
        return 200, {}

    def tag(req: httpx.Request) -> tuple[int, dict[str, Any]]:
        b = json.loads(req.content)
        if b["experiment_id"] == "0":
            state["exp0"]["tags"][b["key"]] = b["value"]
        return 200, {}

    def create(req: httpx.Request) -> tuple[int, dict[str, Any]]:
        state["bench"] = True
        return 200, {"experiment_id": "1"}

    def models(req: httpx.Request) -> tuple[int, dict[str, Any]]:
        m = state["model"]
        return 200, {"models": [m] if m else []}

    def model_create(req: httpx.Request) -> tuple[int, dict[str, Any]]:
        state["model"] = {"info": {"model_id": "m-1", "status": "LOGGED_MODEL_PENDING", "tags": []}}
        return 200, {"model": state["model"]}

    def model_tags(req: httpx.Request) -> tuple[int, dict[str, Any]]:
        state["model"]["info"]["tags"] = json.loads(req.content)["tags"]
        return 200, {}

    return {("GET", "/api/2.0/mlflow/experiments/get"): get,
            ("GET", "/api/2.0/mlflow/experiments/get-by-name"): by_name,
            ("POST", "/api/2.0/mlflow/experiments/update"): update,
            ("POST", "/api/2.0/mlflow/experiments/set-experiment-tag"): tag,
            ("POST", "/api/2.0/mlflow/experiments/create"): create,
            ("POST", "/api/2.0/mlflow/logged-models/search"): models,
            ("POST", "/api/2.0/mlflow/logged-models"): model_create,
            ("PATCH", "/api/2.0/mlflow/logged-models/m-1"): (200, {}),
            ("PATCH", "/api/2.0/mlflow/logged-models/m-1/tags"): model_tags}


async def test_setup_writes_the_difference_then_nothing_the_same_day() -> None:
    boot = require("reader.obs.bootstrap")
    state = {"exp0": {"name": "Default", "tags": {}}, "bench": False, "model": None}
    mlflow = Mlflow(_experiments(state))
    key = "ab" * 32
    async with mlflow.client() as c:
        await boot.ensure_setup(c, VEC6, version_key=key, inputs={"prompt_set_sha256": "x"}, day="2026-11-25")
        first = mlflow.writes()
        mlflow.requests.clear()
        await boot.ensure_setup(c, VEC6, version_key=key, inputs={"prompt_set_sha256": "x"}, day="2026-11-25")
        assert mlflow.writes() == []  # 0 writes on the same UTC day (round 10)
        await boot.ensure_setup(c, VEC6, version_key=key, inputs={"prompt_set_sha256": "x"}, day="2026-11-26")
        assert mlflow.writes() == ["PATCH /api/2.0/mlflow/logged-models/m-1/tags"]  # the day's last_started
    assert "POST /api/2.0/mlflow/experiments/update" in first
    assert "POST /api/2.0/mlflow/experiments/create" in first
    assert "PATCH /api/2.0/mlflow/logged-models/m-1" in first  # finalized READY (round 10)
    assert state["exp0"]["name"] == "reader"
    assert state["exp0"]["tags"]["reader.managed_by"] == "obs.setup"


# --- FR-OBS-22, FR-OBS-23: the upload ---


def _set_text(name: str = "bench-tutor-v2.yaml") -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _search_result(**over: Any) -> dict[str, Any]:
    ids = [q["id"] for q in load_yaml(REPO / "spikes/embeddings-local/questions.yaml")["questions"]]
    keep = [i for i in ids if i not in VEC5["bench_sets"]["search-bench-v1"]["exclude"]]
    per = [{"id": i, "rank": (1 if n % 2 == 0 else None), "hit": n % 2 == 0, "top_pages": [1, 2, 3]}
           for n, i in enumerate(keep)]
    base = {"kind": "search", "model": "BAAI/bge-m3", "chunk_chars": 1200, "overlap": 200, "questions": len(per),
            "per_question": per, "recall_at_10": 0.99}
    base.update(over)
    return base


def test_a_pinned_set_and_its_kind() -> None:
    bench = require("reader.obs.bench")
    assert bench.pinned(_set_text()) == ("tutor-bench-v2", "tutor")
    v1 = (REPO / "spikes/embeddings-local/questions.yaml").read_text(encoding="utf-8")
    assert bench.pinned(v1) == ("search-bench-v1", "search")
    assert bench.pinned(v1 + "\n# edited\n") is None
    assert hashlib.sha256(_set_text().encode()).hexdigest() == VEC5["bench_sets"]["tutor-bench-v2"]["sha256"]


def test_scores_are_recomputed_from_registered_records_only() -> None:
    bench = require("reader.obs.bench")
    v1 = (REPO / "spikes/embeddings-local/questions.yaml").read_text(encoding="utf-8")
    upload = bench.validate(v1, _search_result())
    assert upload.kind == "search" and upload.set_name == "search-bench-v1" and upload.model == "BAAI/bge-m3"
    n = len(_search_result()["per_question"])
    hits = sum(1 for k in range(n) if k % 2 == 0)
    assert upload.scores["hit_at_1"] == pytest.approx(hits / n)
    assert "recall_at_10" not in upload.scores  # the file's own aggregate is never stored (R4)


@pytest.mark.parametrize(("change", "code"), [
    ({"kind": "tutor"}, "bench_invalid"),  # the result's kind differs from the set's
    ({"model": "not a model"}, "bench_invalid"),
    ({"unknown_key": 1}, "bench_invalid"),
    ({"note": CANARIES[3]}, "bench_invalid"),  # prose is an unknown key, refused before anything is stored
])
def test_invalid_results_are_refused(change: dict[str, Any], code: str) -> None:
    bench = require("reader.obs.bench")
    v1 = (REPO / "spikes/embeddings-local/questions.yaml").read_text(encoding="utf-8")
    with pytest.raises(bench.BenchError) as e:
        bench.validate(v1, _search_result(**change))
    assert e.value.code == code


def test_an_unpinned_set_and_an_unknown_id_are_refused() -> None:
    bench = require("reader.obs.bench")
    with pytest.raises(bench.BenchError) as e:
        bench.validate("questions: []\n" + CANARIES[0], _search_result())
    assert e.value.code == "set_not_pinned"
    v1 = (REPO / "spikes/embeddings-local/questions.yaml").read_text(encoding="utf-8")
    bad = _search_result()
    bad["per_question"][0]["id"] = "q04"  # excluded: not a registered record
    with pytest.raises(bench.BenchError) as e:
        bench.validate(v1, bad)
    assert e.value.code == "bench_invalid"


def test_dataset_records_hold_exactly_the_declared_keys() -> None:
    bench = require("reader.obs.bench")
    records = bench.dataset_records(_set_text())
    assert len(records) == 40
    for r in records:
        assert set(r) == {"inputs", "expectations"}
        assert set(r["inputs"]) == {"question_id", "question", "chapter"}
        assert isinstance(r["inputs"]["chapter"], int) and 1 <= r["inputs"]["chapter"] <= 23
        assert set(r["expectations"]) == {"gold_pages"}


# --- FR-OBS-24: label mirrors ---


async def test_a_label_mirror_posts_once_and_overrides_on_relabel() -> None:
    labels = require("reader.obs.labels")
    trace = "tr-" + "b" * 32
    mlflow = Mlflow({("GET", "/ajax-api/3.0/mlflow/traces/get"): {"trace": {"trace_info": {"assessments": []}}},
                     ("POST", f"/api/3.0/mlflow/traces/{trace}/assessments"):
                         {"assessment": {"assessment_id": "a-2"}}})
    label = labels.Label(target="lesson", target_id=LESSON, ok=False, reason="wrong_fact", revision=2,
                         assessment_id="a-1")
    async with mlflow.client() as c:
        posted = await labels.mirror(label, job_revision=2, trace_ref=trace, client=c)
    assert posted == "a-2"
    body = mlflow.bodies()[-1]["assessment"]
    assert body["assessment_name"] == "owner.lesson_ok" and body["overrides"] == "a-1"
    assert body["source"] == {"source_type": "HUMAN", "source_id": "owner"}
    assert body["feedback"] == {"value": False}
    assert body["metadata"] == {"lesson_id": LESSON, "reason": "wrong_fact", "revision": "2"}
    assert "rationale" not in body


async def test_an_old_revision_or_an_existing_mirror_makes_no_post() -> None:
    labels = require("reader.obs.labels")
    trace = "tr-" + "b" * 32
    there = {"assessment_id": "a-9", "assessment_name": "owner.lesson_ok", "metadata": {"revision": "2"}}
    mlflow = Mlflow({("GET", "/ajax-api/3.0/mlflow/traces/get"):
                     {"trace": {"trace_info": {"assessments": [there]}}}})
    label = labels.Label(target="lesson", target_id=LESSON, ok=True, reason=None, revision=2, assessment_id=None)
    async with mlflow.client() as c:
        assert await labels.mirror(label, job_revision=1, trace_ref=trace, client=c) is None  # below the row's
        assert mlflow.requests == []
        assert await labels.mirror(label, job_revision=2, trace_ref=trace, client=c) == "a-9"  # found by revision
    assert [r.method for r in mlflow.requests] == ["GET"]


# --- FR-OBS-25: alerts ---


def test_alerts_from_postgres_rows_only() -> None:
    alerts = require("reader.obs.alerts")
    got = alerts.compute(
        task_spend_today={"tutor": 2.5, "draft": 0.1}, task_daily_caps={"tutor": 3.0, "draft": 3.0},
        open_runs=[(RUN, 7.0, 8.0)], dead_jobs={"pipeline.stage": 2},
        bench=[("search", "search-bench-v1", "BAAI/bge-m3", {"hit_at_10": 0.70}),
               ("search", "search-bench-v1", "BAAI/bge-m3", {"hit_at_10": 0.60}),
               ("search", "search-bench-v1", "other/model", {"hit_at_10": 0.10})],
        stack={"mlflow": (True, datetime(2026, 11, 25, 11, 0, tzinfo=UTC)),
               "rotel": (False, datetime(2026, 11, 25, 11, 50, tzinfo=UTC))},
        now=datetime(2026, 11, 25, 12, 0, tzinfo=UTC))
    codes = sorted((a["code"], a["subject"]) for a in got)
    assert codes == sorted([
        ("daily_spend_high", "tutor"), ("run_over_cap", RUN), ("dead_jobs", "pipeline.stage"),
        ("bench_regressed", "search/search-bench-v1/BAAI/bge-m3"), ("obs_unhealthy", "rotel"),
    ])  # mlflow ok and 60 minutes old (< 75): no alert; the other model has no previous upload
    assert {a["code"] for a in got} <= {e["code"] for e in VEC5["alerts"]}


# --- FR-OBS-31: retention of Runs and LoggedModels ---


async def test_retention_deletes_old_runs_and_unused_versions() -> None:
    runs = require("reader.obs.runs")
    old, new = 1_790_000_000_000, 1_795_900_000_000
    mlflow = Mlflow({
        ("POST", "/api/2.0/mlflow/runs/search"): {"runs": [
            {"info": {"run_id": "old", "end_time": old}, "data": {}},
            {"info": {"run_id": "new", "end_time": new}, "data": {}}]},
        ("POST", "/api/2.0/mlflow/logged-models/search"): {"models": [
            {"info": {"model_id": "m-old", "tags": [{"key": "reader.version_key", "value": "c" * 12},
                                                     {"key": "reader.last_started", "value": "2026-09-01"}]}},
            {"info": {"model_id": "m-run", "tags": [{"key": "reader.version_key", "value": "a" * 12},
                                                     {"key": "reader.last_started", "value": "2026-09-01"}]}}]},
    })
    async with mlflow.client() as c:
        await runs.prune(c, now=datetime(2026, 11, 25, tzinfo=UTC), retention_days=30, running_key="a" * 12)
    assert ("POST /api/2.0/mlflow/runs/delete", {"run_id": "old"}) in list(zip(mlflow.paths(), mlflow.bodies(),
                                                                               strict=True))
    deletes = [p for p in mlflow.paths() if p.startswith("DELETE")]
    assert deletes == ["DELETE /api/2.0/mlflow/logged-models/m-old"]  # the running key is kept


# --- FR-OBS-32: the health probe ---


async def test_health_probe_records_mlflow_and_rotel() -> None:
    health = require("reader.obs.health")
    def rotel(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400 if req.url.host == "rotel" else 200, json={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(rotel)) as c:
        got = await health.probe(c)
    assert got == {"mlflow": (True, "healthy"), "rotel": (True, "listening")}
    assert health.KIND_DECL.name == "obs.health" and health.KIND_DECL.traced is False

    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=req)
    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as c:
        got = await health.probe(c)
    assert got == {"mlflow": (False, "unreachable"), "rotel": (False, "unreachable")}


# --- INV-OBS-2 (phase 2): no canary reaches any body ---


async def test_no_canary_in_any_phase2_body() -> None:
    runs = require("reader.obs.runs")
    mlflow = Mlflow({("POST", "/api/2.0/mlflow/runs/search"): _search_reply([]),
                     ("POST", "/api/2.0/mlflow/runs/create"): {"run": {"info": {"run_id": "r1"}, "data": {}}},
                     ("POST", "/ajax-api/3.0/mlflow/traces/search"): _traces_reply(1)})
    async with mlflow.client() as c:
        with pytest.raises(ValueError):  # a canary in a tag value fails VEC-OBS-5 before any request
            await runs.sync(_snapshot(tags={"reader.lesson.id": CANARIES[0]}), c)
        assert mlflow.writes() == []
        await runs.sync(_snapshot(), c)
    assert not any(any(k in (r.content or b"").decode() for k in CANARIES) for r in mlflow.requests)


# --- INV-OBS-3 (phase 2): a failing enqueue never changes the outcome ---


async def test_a_failing_runs_enqueue_is_logged_and_swallowed(monkeypatch: pytest.MonkeyPatch, caplog: Any) -> None:
    runs = require("reader.obs.runs")
    import logging

    import reader.jobs

    class Down:
        async def enqueue(self, kind: str, args: Any, schedule_in: int = 0) -> None:
            raise RuntimeError("queue down")

    monkeypatch.setattr(reader.jobs, "running", lambda: Down())
    import reader.obs

    monkeypatch.setitem(reader.obs._STATE, "provider", object())
    with caplog.at_level(logging.WARNING, logger="reader.obs"):
        await runs.enqueue_end("compile", RUN, 1)  # never raises
    assert "obs runs_enqueue_failed kind=compile" in [r.getMessage() for r in caplog.records]
