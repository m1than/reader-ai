"""SRV-OBS [unit]: the worker's two MLflow writes under ADR-0035 — code assessments (`obs.feedback`, FR-OBS-14) and the
prompt registry (`obs.prompts`, FR-OBS-15) — their bodies built only from VEC-OBS-4's catalogue and the repository's
static files, their failure paths, and the canary over every request body and job args (INV-OBS-2).

@trace FR-OBS-14 FR-OBS-15 INV-OBS-2 INV-OBS-8 VEC-OBS-4

MLflow is an in-test httpx.MockTransport that records every request.
"""

from __future__ import annotations

import json
import logging
import re
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from .conftest import FIXTURES, load_yaml, require

VEC4 = load_yaml(FIXTURES / "analytics.yaml")
CANARIES = ("bookcanary-7f3a9c", "questioncanary-1b2c", "answercanary-9d8e", "systemcanary-4f5a", "rt_canary", "sk-ant-")


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Mlflow:
    """An in-test MLflow: answers per (method, path) from `replies`, records every request."""

    def __init__(self, replies: dict[tuple[str, str], Any]) -> None:
        self.replies, self.requests = replies, []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.get((request.method, request.url.path), (200, {}))
        status, body = reply if isinstance(reply, tuple) else (200, reply)
        return httpx.Response(status, json=body)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def _args(**over: Any) -> Any:
    feedback = require("reader.obs.feedback")
    base = {"job_id": 104, "key": "pipeline.stage:" + "a" * 64, "name": "fact_check.passed", "value": True,
            "metadata": {"claims": 14, "supported": 13}}
    return feedback.FeedbackArgs.model_validate({**base, **over})


def test_kind_declarations() -> None:
    feedback, prompts = require("reader.obs.feedback"), require("reader.obs.prompts")
    k = feedback.KIND_DECL
    assert (k.name, k.queue, k.key, k.max_attempts, k.timeout_s) == ("obs.feedback", "obs", ("job_id", "name"), 3, 30)
    assert feedback.TraceMissing in k.transient and httpx.TransportError in k.transient
    assert feedback.SCHEDULE_IN_S == 60 and feedback.HTTP_TIMEOUT_S == 10.0
    p = prompts.KIND_DECL
    assert (p.name, p.queue, p.key, p.max_attempts) == ("obs.prompts", "obs", ("digest",), 3)
    assert prompts.LOOKUP_TIMEOUT_S == 2.0 and prompts.MISS_TTL_S == 600.0
    assert sorted(VEC4["job_kinds"]) == ["obs.align", "obs.eval", "obs.eval_run", "obs.eval_sample", "obs.feedback",
                                         "obs.judge_score", "obs.prompts"]


def test_body_is_the_catalogue_only() -> None:
    feedback = require("reader.obs.feedback")
    body = feedback.body("verify.lab_passed", False, {"cells": 12, "cells_ok": 10}, "tr-" + "b" * 32)
    assert body == {"assessment": {
        "assessment_name": "verify.lab_passed", "trace_id": "tr-" + "b" * 32,
        "source": {"source_type": "CODE", "source_id": "reader.pipeline.verify"},
        "feedback": {"value": False}, "metadata": {"cells": "12", "cells_ok": "10"},
    }}
    assert "rationale" not in json.dumps(body)
    for name, value, meta in (
        ("freshness.verdict", True, {}),  # not in this revision's catalogue
        ("fact_check.passed", 1, {}),  # a bool rule takes a bool only
        ("tutor.citation_share", 1.5, {}),  # outside 0..1
        ("tutor.citation_share", True, {}),
        ("fact_check.passed", True, {"note": 1}),  # an unlisted metadata key
        ("fact_check.passed", True, {"claims": 10001}),  # outside its range
        ("fact_check.passed", True, {"claims": "14"}),
        ("eval.relevance", 0.5, {}),  # an LLM_JUDGE entry: only eval_body builds it (FR-OBS-14 as amended)
        ("eval.sufficiency", 0.5, {"score_id": 1}),
    ):
        with pytest.raises(feedback.CatalogError):
            feedback.body(name, value, meta, "tr-x")


# --- ADR-0039: LLM_JUDGE assessments (FR-OBS-14 as amended, INV-OBS-8) ---

SCORE = "6f2c1a9e-0b4d-4c8e-9f12-6a7b8c9d0e1f"
TRACE = "tr-" + "e" * 32
JUDGE_NAMES = [e["name"] for e in VEC4["assessments"] if e["source_type"] == "LLM_JUDGE"]


def test_eval_body_is_the_catalogue_only() -> None:
    feedback = require("reader.obs.feedback")
    body = feedback.eval_body("eval.sufficiency", 0.7, SCORE, "judge.eval.sufficiency@0123456789ab", TRACE)
    assert body == {"assessment": {
        "assessment_name": "eval.sufficiency", "trace_id": TRACE,
        "source": {"source_type": "LLM_JUDGE", "source_id": "judge.eval.sufficiency@0123456789ab"},
        "feedback": {"value": 0.7}, "metadata": {"score_id": SCORE},
    }}
    assert feedback.eval_body("eval.relevance", 1, SCORE, "judge.eval.relevance@0123456789ab", TRACE)[
        "assessment"]["feedback"] == {"value": 1.0}
    good = ("eval.relevance", 0.5, SCORE, "judge.eval.relevance@0123456789ab", TRACE)
    for i, bad in (
        (0, "fact_check.passed"),  # a CODE entry: only body builds it
        (0, "owner.judge_find_ok"),  # HUMAN names are obs.label's, not this catalogue
        (0, "eval.unknown"),
        (1, 1.01), (1, -0.1), (1, float("nan")), (1, float("inf")), (1, True), (1, "0.5"),
        (2, "not-a-uuid"), (2, SCORE.upper()), (2, SCORE + "\n"),
        (3, "judge.eval.relevance@0123456789AB"), (3, "judge.eval.relevance@0123"),
        (3, "judge.find@0123456789ab"),
        (3, "judge.eval.sufficiency@0123456789ab"),  # another slot's judge string for this name
        (4, "tr-x"),
    ):
        args = list(good)
        args[i] = bad
        with pytest.raises(feedback.CatalogError):
            feedback.eval_body(*args)


_JUDGE_TEXT = st.one_of(st.sampled_from([*CANARIES, *JUDGE_NAMES, *[e["name"] for e in VEC4["assessments"]]]),
                        st.text(max_size=30))
_SOURCE = st.one_of(st.sampled_from([f"judge.{n}@0123456789ab" for n in JUDGE_NAMES] + ["reader.pipeline.verify"]),
                    st.text(max_size=40))


@settings(max_examples=300)
@given(name=_JUDGE_TEXT,
       value=st.one_of(st.booleans(), st.floats(allow_nan=True), st.integers(), st.text(max_size=20)),
       score=st.one_of(st.just(SCORE), st.text(max_size=40)), source=_SOURCE,
       meta=st.dictionaries(_JUDGE_TEXT, st.one_of(st.integers(), st.text(max_size=10)), max_size=3))
def test_writer_by_source(name: str, value: Any, score: str, source: str, meta: dict[str, Any]) -> None:
    """INV-OBS-8: only catalogue entries produce a body; CODE entries only through `body` (obs.feedback), LLM_JUDGE
    entries only through `eval_body` (obs.judge_score); an LLM_JUDGE body has a value in [0, 1], no rationale and
    metadata {score_id} only."""
    feedback = require("reader.obs.feedback")
    entries = {e["name"]: e for e in VEC4["assessments"]}
    for build, args, writer in ((feedback.body, (name, value, meta, TRACE), "obs.feedback"),
                                (feedback.eval_body, (name, value, score, source, TRACE), "obs.judge_score")):
        try:
            made = build(*args)
        except feedback.CatalogError:
            continue
        entry = entries[name]
        assert entry["writer"] == writer
        a = made["assessment"]
        assert a["source"]["source_type"] == entry["source_type"]
        assert "rationale" not in json.dumps(made)
        if writer == "obs.judge_score":
            assert isinstance(a["feedback"]["value"], float) and 0 <= a["feedback"]["value"] <= 1
            assert a["metadata"] == {"score_id": score}
            assert re.fullmatch(entry["source_id_pattern"], a["source"]["source_id"])
        assert not any(c in json.dumps(made) for c in CANARIES)


def test_lookup_matches_name_source_and_score_id() -> None:
    """FR-OBS-14 as amended: an existing assessment counts only with the same name, source_id and, for LLM_JUDGE,
    metadata.score_id; so a retry never posts twice and a new judge version posts next to the old one."""
    feedback = require("reader.obs.feedback")
    made = feedback.eval_body("eval.relevance", 0.5, SCORE, "judge.eval.relevance@0123456789ab", TRACE)
    same = {"assessment_name": "eval.relevance", "source": {"source_type": "LLM_JUDGE",
                                                            "source_id": "judge.eval.relevance@0123456789ab"},
            "metadata": {"score_id": SCORE}}
    assert feedback.already([same], made) is True
    assert feedback.already([{**same, "metadata": {"score_id": "7" + SCORE[1:]}}], made) is False
    assert feedback.already([{**same, "source": {"source_id": "judge.eval.relevance@ba9876543210"}}], made) is False
    assert feedback.already([{**same, "assessment_name": "eval.sufficiency"}], made) is False
    assert feedback.already([], made) is False
    code = feedback.body("verify.passed", True, {}, TRACE)
    there = {"assessment_name": "verify.passed", "source": {"source_id": "reader.pipeline.verify"}}
    assert feedback.already([there], code) is True  # CODE: name and source decide, as before


_TEXT = st.one_of(st.sampled_from([*CANARIES, *[e["name"] for e in VEC4["assessments"]]]), st.text(max_size=30))
_VALUE = st.one_of(st.booleans(), st.floats(allow_nan=True), st.integers(), st.text(max_size=20))


@settings(max_examples=200)
@given(name=_TEXT, value=_VALUE, meta=st.dictionaries(_TEXT, st.one_of(st.integers(), st.text(max_size=10)), max_size=3))
def test_only_catalogue_entries_produce_a_body(name: str, value: Any, meta: dict[str, Any]) -> None:  # INV-OBS-2
    feedback = require("reader.obs.feedback")
    entries = {e["name"]: e for e in VEC4["assessments"]}
    try:
        body = feedback.body(name, value, meta, "tr-" + "c" * 32)
    except feedback.CatalogError:
        return
    entry = entries[name]  # a body exists only for a catalogue name
    assert entry["source_type"] == "CODE"
    a = body["assessment"]
    assert set(a) == {"assessment_name", "trace_id", "source", "feedback", "metadata"}
    assert a["source"] == {"source_type": "CODE", "source_id": entry["source_id"]}
    assert set(a["metadata"]) <= set(entry["metadata"])
    assert all(re.fullmatch(r"-?[0-9]+", v) for v in a["metadata"].values())
    assert not any(c in json.dumps(body) for c in CANARIES)


async def test_post_creates_once_and_skips_an_existing_one() -> None:
    feedback = require("reader.obs.feedback")
    args = _args()
    trace = feedback.trace_ref(args.job_id, args.key)
    assert re.fullmatch(r"tr-[0-9a-f]{32}", trace)
    empty = Mlflow({("GET", "/ajax-api/3.0/mlflow/traces/get"): {"trace": {"trace_info": {"assessments": []}}}})
    async with empty.client() as c:
        assert await feedback.post(args, c, last=False) is True
    get, post = empty.requests
    assert get.url.params["trace_id"] == trace
    assert post.url.path == f"/ajax-api/3.0/mlflow/traces/{trace}/assessments"
    assert json.loads(post.content) == feedback.body("fact_check.passed", True, {"claims": 14, "supported": 13}, trace)
    there = {"assessment_name": "fact_check.passed", "source": {"source_id": "reader.pipeline.fact_check"}}
    existing = Mlflow({("GET", "/ajax-api/3.0/mlflow/traces/get"): {"trace": {"trace_info": {"assessments": [there]}}}})
    async with existing.client() as c:
        assert await feedback.post(args, c, last=False) is False
    assert [r.method for r in existing.requests] == ["GET"]  # idempotent: no second assessment


@pytest.mark.parametrize("there", [
    # verify.passed and verify.lab_passed share the source_id reader.pipeline.verify: the name decides
    {"assessment_name": "verify.passed", "source": {"source_id": "reader.pipeline.verify"}},
    # the same name from another source is not ours
    {"assessment_name": "verify.lab_passed", "source": {"source_id": "reader.other"}},
])
async def test_idempotency_needs_the_same_name_and_source(there: dict[str, Any]) -> None:
    feedback = require("reader.obs.feedback")
    args = _args(name="verify.lab_passed", value=True, metadata={"cells": 12, "cells_ok": 12})
    mlflow = Mlflow({("GET", "/ajax-api/3.0/mlflow/traces/get"): {"trace": {"trace_info": {"assessments": [there]}}}})
    async with mlflow.client() as c:
        assert await feedback.post(args, c, last=False) is True
    assert [r.method for r in mlflow.requests] == ["GET", "POST"]


async def test_404_is_transient_until_the_last_attempt(caplog: Any) -> None:
    feedback = require("reader.obs.feedback")
    missing = Mlflow({("GET", "/ajax-api/3.0/mlflow/traces/get"): (404, {})})
    async with missing.client() as c:
        with pytest.raises(feedback.TraceMissing):
            await feedback.post(_args(), c, last=False)
        with caplog.at_level(logging.INFO, logger="reader.obs"):
            assert await feedback.post(_args(), c, last=True) is False
    got = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert (logging.INFO, "obs feedback_no_trace name=fact_check.passed") in got
    assert [r.method for r in missing.requests] == ["GET", "GET"]  # no POST after a 404 GET


async def test_post_404_is_transient_then_a_warning(caplog: Any) -> None:
    feedback = require("reader.obs.feedback")
    trace = feedback.trace_ref(104, "pipeline.stage:" + "a" * 64)
    gone = Mlflow({
        ("GET", "/ajax-api/3.0/mlflow/traces/get"): {"trace": {"trace_info": {"assessments": []}}},
        ("POST", f"/ajax-api/3.0/mlflow/traces/{trace}/assessments"): (404, {}),
    })
    async with gone.client() as c:
        with pytest.raises(feedback.TraceMissing):
            await feedback.post(_args(), c, last=False)
        with caplog.at_level(logging.INFO, logger="reader.obs"):
            assert await feedback.post(_args(), c, last=True) is False
    assert (logging.WARNING, "obs feedback_no_trace name=fact_check.passed") in [
        (r.levelno, r.getMessage()) for r in caplog.records
    ]


def _patch_client(monkeypatch: pytest.MonkeyPatch, mlflow: Mlflow) -> None:
    """Every httpx.AsyncClient the code under test opens talks to `mlflow`."""
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: original(transport=httpx.MockTransport(mlflow.handler)))


@pytest.mark.parametrize(("attempts", "raises"), [(0, True), (1, True), (2, False)])
async def test_handler_last_attempt_is_the_third(monkeypatch: pytest.MonkeyPatch, attempts: int, raises: bool) -> None:
    feedback = require("reader.obs.feedback")
    from reader.obs import setup

    monkeypatch.setattr(setup, "current_settings", lambda: setup.Settings(True, 30))
    missing = Mlflow({("GET", "/ajax-api/3.0/mlflow/traces/get"): (404, {})})
    _patch_client(monkeypatch, missing)
    if raises:
        with pytest.raises(feedback.TraceMissing):
            await feedback.handle(_args(), None, SimpleNamespace(attempts=attempts))
    else:
        await feedback.handle(_args(), None, SimpleNamespace(attempts=attempts))  # ends without a request
    assert len(missing.requests) == 1


async def test_other_answers() -> None:
    feedback = require("reader.obs.feedback")
    for status, error in ((400, feedback.FeedbackRefused), (500, httpx.HTTPStatusError)):
        bad = Mlflow({("GET", "/ajax-api/3.0/mlflow/traces/get"): (status, {})})
        async with bad.client() as c:
            with pytest.raises(error):
                await feedback.post(_args(), c, last=True)


async def test_handler_does_nothing_with_tracing_off(monkeypatch: pytest.MonkeyPatch) -> None:
    feedback = require("reader.obs.feedback")
    from reader.obs import setup

    monkeypatch.setattr(setup, "current_settings", lambda: setup.Settings(False, 30))
    sent: list[Any] = []
    monkeypatch.setattr(httpx.AsyncClient, "send", lambda *a, **k: sent.append(a))
    await feedback.handle(_args(), None, SimpleNamespace(attempts=0))
    assert sent == []


async def test_record_enqueues_and_never_raises(monkeypatch: pytest.MonkeyPatch, caplog: Any) -> None:
    feedback = require("reader.obs.feedback")
    import reader.jobs

    calls: list[Any] = []

    class Jobs:
        async def enqueue(self, kind: str, args: Any, schedule_in: int = 0) -> None:
            calls.append((kind, args, schedule_in))

    import reader.obs

    monkeypatch.setattr(reader.jobs, "running", lambda: Jobs())
    ctx = SimpleNamespace(job_id=7, key="tutor.answer:" + "d" * 64, attempts=0)
    monkeypatch.setitem(reader.obs._STATE, "provider", None)
    await feedback.record(ctx, "tutor.citations_valid", True, {"total": 3, "valid": 3})
    assert calls == []  # tracing off: nothing to assess, nothing enqueued
    monkeypatch.setitem(reader.obs._STATE, "provider", object())
    await feedback.record(ctx, "tutor.citations_valid", True, {"total": 3, "valid": 3})
    assert calls == [("obs.feedback", {"job_id": 7, "key": ctx.key, "name": "tutor.citations_valid", "value": True,
                                       "metadata": {"total": 3, "valid": 3}}, 60)]
    assert not any(c in json.dumps(calls) for c in CANARIES)  # INV-OBS-2: the args hold ids and numbers only
    calls.clear()
    await feedback.record(ctx, CANARIES[0], True)  # the catalogue check comes first, even with a working queue
    await feedback.record(ctx, "tutor.citations_valid", True, {"note": 1})
    assert calls == []

    def down() -> Any:
        raise RuntimeError("no worker")

    monkeypatch.setattr(reader.jobs, "running", down)
    with caplog.at_level(logging.WARNING, logger="reader.obs"):
        await feedback.record(ctx, "tutor.citations_valid", True)
        await feedback.record(ctx, CANARIES[0], True)  # not in the catalogue: refused before any enqueue
    lines = [r.getMessage() for r in caplog.records]
    assert "obs feedback_enqueue_failed name=tutor.citations_valid" in lines
    assert "obs feedback_enqueue_failed name=<invalid>" in lines
    assert not any(CANARIES[0] in line for line in lines)


# --- FR-OBS-15: the prompt registry ---

SCANNER = re.compile(r"(sk-ant-[A-Za-z0-9_-]{10,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----)")


def test_prompt_list_gains_the_judge_files() -> None:
    """FR-OBS-15 as edited: the list is VEC-OBS-4 `prompts`, with SRV-OBS's six judge files (FR-OBS-43); SRV-AGENT's
    judge_find.md is not on this branch yet and is not listed."""
    prompts = require("reader.obs.prompts")
    judge = {k: v for k, v in prompts.PROMPTS.items() if k.startswith("reader.judge.")}
    assert sorted(judge) == sorted(
        ["reader.judge.eval.relevance", "reader.judge.eval.sufficiency", "reader.judge.eval.groundedness",
         "reader.judge.eval.self_contained", "reader.judge.eval.citation_supports_claim", "reader.judge.align"])
    assert all(v.startswith("server/src/reader/obs/judge_prompts/judge_") for v in judge.values())
    assert "reader.judge.find" not in prompts.PROMPTS


def test_prompt_files_are_static_and_clean() -> None:
    prompts = require("reader.obs.prompts")
    assert set(VEC4["prompts"]) == set(prompts.PROMPTS)
    for name in prompts.PROMPTS:
        text, sha = prompts.template(name)
        assert text and len(sha) == 64
        assert "{{" not in text and "}}" not in text, name  # nothing a renderer would fill with book text
        assert SCANNER.search(text) is None, name


async def test_register_creates_a_version_only_for_a_new_sha() -> None:
    prompts = require("reader.obs.prompts")
    text, sha = prompts.template("reader.tutor")
    fresh = Mlflow({
        ("GET", "/api/2.0/mlflow/model-versions/search"): {"model_versions": []},
        ("POST", "/api/2.0/mlflow/model-versions/create"): {"model_version": {"version": "1"}},
    })
    async with fresh.client() as c:
        assert await prompts.register(c, "reader.tutor", "0.1.0") == 1
    search, model, version = fresh.requests
    assert search.url.params["filter"] == "name='reader.tutor' AND tags.`mlflow.prompt.is_prompt` = 'true'"
    assert json.loads(model.content) == {"name": "reader.tutor",
                                         "tags": [{"key": "mlflow.prompt.is_prompt", "value": "true"}]}
    body = json.loads(version.content)
    tags = {t["key"]: t["value"] for t in body["tags"]}
    assert body["source"] == "dummy-source"
    assert tags == {"mlflow.prompt.is_prompt": "true", "mlflow.prompt.text": text, "_mlflow_prompt_type": "text",
                    "reader.prompt_sha256": sha, "reader.version": "0.1.0"}
    for request in fresh.requests:
        assert not any(c in request.content.decode() for c in CANARIES)  # INV-OBS-2
    known = Mlflow({("GET", "/api/2.0/mlflow/model-versions/search"): {"model_versions": [
        {"version": "4", "tags": [{"key": "reader.prompt_sha256", "value": sha}]}]}})
    async with known.client() as c:
        assert await prompts.register(c, "reader.tutor", "0.2.0") == 4
    assert [r.method for r in known.requests] == ["GET"]


def _found(sha: str, number: str = "3") -> dict[str, Any]:
    return {"model_versions": [{"version": number, "tags": [{"key": "reader.prompt_sha256", "value": sha}]}]}


async def test_version_of_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-OBS-18: tracing off or an unknown name returns None before any request (MLflow reachable here); a
    lookup uses LOOKUP_TIMEOUT_S; a found version is cached; a miss is cached for MISS_TTL_S."""
    prompts = require("reader.obs.prompts")
    import reader.obs

    _, sha = prompts.template("reader.tutor")
    monkeypatch.setattr(prompts, "_CACHE", {})
    monkeypatch.setattr(prompts, "_MISSES", {})
    mlflow = Mlflow({("GET", "/api/2.0/mlflow/model-versions/search"): _found(sha)})
    _patch_client(monkeypatch, mlflow)
    monkeypatch.setitem(reader.obs._STATE, "provider", None)
    assert await prompts.version_of("reader.tutor") is None
    monkeypatch.setitem(reader.obs._STATE, "provider", object())
    assert await prompts.version_of("reader.verify") is None  # no such template
    assert mlflow.requests == []
    assert await prompts.version_of("reader.tutor") == 3
    assert await prompts.version_of("reader.tutor") == 3  # cached: one request
    (lookup,) = mlflow.requests
    assert lookup.extensions["timeout"]["read"] == prompts.LOOKUP_TIMEOUT_S
    # A miss: one request, then none until the TTL passes.
    monkeypatch.setattr(prompts, "_CACHE", {})
    clock = [1000.0]
    monkeypatch.setattr(prompts.time, "monotonic", lambda: clock[0])
    mlflow.replies[("GET", "/api/2.0/mlflow/model-versions/search")] = {"model_versions": []}
    assert await prompts.version_of("reader.tutor") is None
    assert await prompts.version_of("reader.tutor") is None
    assert len(mlflow.requests) == 2
    clock[0] += prompts.MISS_TTL_S + 1
    assert await prompts.version_of("reader.tutor") is None
    assert len(mlflow.requests) == 3


async def test_version_of_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts = require("reader.obs.prompts")
    import reader.obs

    monkeypatch.setattr(prompts, "_CACHE", {})
    monkeypatch.setattr(prompts, "_MISSES", {})
    monkeypatch.setitem(reader.obs._STATE, "provider", object())

    tries: list[str] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        tries.append(request.url.path)
        raise httpx.ConnectError("down", request=request)

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(refuse)))
    assert await prompts.version_of("reader.tutor") is None
    # A failed lookup is a cached miss too (code review round 2): no second try within MISS_TTL_S.
    assert await prompts.version_of("reader.tutor") is None
    assert len(tries) == 1 and "reader.tutor" in prompts._MISSES


async def test_registration_clears_a_cached_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    """Code review round 2: after obs.prompts registers a template, version_of answers from the cache at once,
    even within a miss's TTL."""
    prompts = require("reader.obs.prompts")
    import time

    import reader.obs
    from reader.obs import setup

    monkeypatch.setattr(setup, "current_settings", lambda: setup.Settings(True, 30))
    monkeypatch.setattr(prompts, "_CACHE", {})
    monkeypatch.setattr(prompts, "_MISSES", dict.fromkeys(prompts.PROMPTS, time.monotonic() + 600))
    monkeypatch.setitem(reader.obs._STATE, "provider", object())
    up = Mlflow({("GET", "/api/2.0/mlflow/model-versions/search"): {"model_versions": []},
                 ("POST", "/api/2.0/mlflow/model-versions/create"): {"model_version": {"version": "2"}}})
    _patch_client(monkeypatch, up)
    await prompts.handle(prompts.PromptsArgs(version="0.1.0", digest=prompts.digest("2026-11-24")), None, None)
    assert prompts._MISSES == {}
    before = len(up.requests)
    assert await prompts.version_of("reader.tutor") == 2
    assert len(up.requests) == before  # answered from the cache, no lookup


async def test_digest_keys_on_templates_and_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-OBS-15: a key that ended dead (MLflow down at start) is retried by the next day's start; changed
    templates get a new key at once; registration itself is idempotent by sha."""
    prompts = require("reader.obs.prompts")
    from datetime import UTC, datetime

    calls: list[Any] = []

    class Jobs:
        async def enqueue(self, kind: str, args: Any, schedule_in: int = 0) -> None:
            calls.append((kind, args))

    day1, day2 = datetime(2026, 11, 24, 9, tzinfo=UTC), datetime(2026, 11, 25, 9, tzinfo=UTC)
    await prompts.on_worker_start(Jobs(), day1)
    await prompts.on_worker_start(Jobs(), day1)
    await prompts.on_worker_start(Jobs(), day2)
    (k1, a1), (_, a2), (_, a3) = calls
    assert k1 == "obs.prompts" and set(a1) == {"version", "digest"}
    assert a1 == a2 and a1["digest"] != a3["digest"]
    assert a1["digest"] == prompts.digest("2026-11-24")
    real = prompts.template
    monkeypatch.setattr(prompts, "template", lambda n: (real(n)[0], "0" * 64) if n == "reader.tutor" else real(n))
    assert prompts.digest("2026-11-24") != a1["digest"]  # a changed template: a new key the same day


async def test_mlflow_down_at_start_up_later(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts = require("reader.obs.prompts")
    from reader.obs import setup

    monkeypatch.setattr(setup, "current_settings", lambda: setup.Settings(True, 30))
    monkeypatch.setattr(prompts, "_CACHE", {})
    args = prompts.PromptsArgs(version="0.1.0", digest=prompts.digest("2026-11-24"))

    up = Mlflow({})
    up.replies[("GET", "/api/2.0/mlflow/model-versions/search")] = {"model_versions": []}
    up.replies[("POST", "/api/2.0/mlflow/model-versions/create")] = {"model_version": {"version": "1"}}
    state = {"down": True}

    def answer(request: httpx.Request) -> httpx.Response:
        if state["down"]:
            raise httpx.ConnectError("down", request=request)
        return up.handler(request)

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(answer)))
    with pytest.raises(httpx.TransportError):  # transient: the key is retried, then dead after 3 attempts
        await prompts.handle(args, None, None)
    assert prompts._CACHE == {}
    shas = {name: prompts.template(name)[1] for name in prompts.PROMPTS}
    state["down"] = False
    later = prompts.PromptsArgs(version="0.1.0", digest=prompts.digest("2026-11-25"))  # the next day's key
    await prompts.handle(later, None, None)
    assert dict.fromkeys(shas, 1) == prompts._CACHE


async def test_prompts_handler_does_nothing_with_tracing_off(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts = require("reader.obs.prompts")
    from reader.obs import setup

    monkeypatch.setattr(setup, "current_settings", lambda: setup.Settings(False, 30))
    mlflow = Mlflow({})
    _patch_client(monkeypatch, mlflow)
    await prompts.handle(prompts.PromptsArgs(version="0.1.0", digest="a" * 64), None, None)
    assert mlflow.requests == []


async def test_start_hook_never_raises(caplog: Any) -> None:
    prompts = require("reader.obs.prompts")

    class Down:
        async def enqueue(self, kind: str, args: Any, schedule_in: int = 0) -> None:
            raise RuntimeError("queue down")

    with caplog.at_level(logging.WARNING, logger="reader.obs"):
        await prompts.on_worker_start(Down())
    assert "obs prompts not enqueued" in [r.getMessage() for r in caplog.records]
