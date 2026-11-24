"""SRV-OBS [unit]: the worker's two MLflow writes under ADR-0035 — code assessments (`obs.feedback`, FR-OBS-14) and the
prompt registry (`obs.prompts`, FR-OBS-15) — their bodies built only from VEC-OBS-4's catalogue and the repository's
static files, their failure paths, and the canary over every request body and job args (INV-OBS-2).

@trace FR-OBS-14 FR-OBS-15 INV-OBS-2 VEC-OBS-4

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
    assert (p.name, p.queue, p.key, p.max_attempts) == ("obs.prompts", "obs", ("version",), 3)
    assert sorted(VEC4["job_kinds"]) == ["obs.feedback", "obs.prompts"]


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
    ):
        with pytest.raises(feedback.CatalogError):
            feedback.body(name, value, meta, "tr-x")


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


async def test_404_is_transient_until_the_last_attempt(caplog: Any) -> None:
    feedback = require("reader.obs.feedback")
    missing = Mlflow({("GET", "/ajax-api/3.0/mlflow/traces/get"): (404, {})})
    async with missing.client() as c:
        with pytest.raises(feedback.TraceMissing):
            await feedback.post(_args(), c, last=False)
        with caplog.at_level(logging.INFO, logger="reader.obs"):
            assert await feedback.post(_args(), c, last=True) is False
    assert "obs feedback_no_trace name=fact_check.passed" in [r.getMessage() for r in caplog.records]


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

    monkeypatch.setattr(reader.jobs, "running", lambda: Jobs())
    ctx = SimpleNamespace(job_id=7, key="tutor.answer:" + "d" * 64, attempts=0)
    await feedback.record(ctx, "tutor.citations_valid", True, {"total": 3, "valid": 3})
    assert calls == [("obs.feedback", {"job_id": 7, "key": ctx.key, "name": "tutor.citations_valid", "value": True,
                                       "metadata": {"total": 3, "valid": 3}}, 60)]
    assert not any(c in json.dumps(calls) for c in CANARIES)  # INV-OBS-2: the args hold ids and numbers only

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
    assert search.url.params["filter"] == "name='reader.tutor'"
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


async def test_version_of_is_none_with_tracing_off_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts = require("reader.obs.prompts")
    import reader.obs

    monkeypatch.setattr(prompts, "_CACHE", {})
    monkeypatch.setitem(reader.obs._STATE, "provider", None)
    assert await prompts.version_of("reader.tutor") is None
    monkeypatch.setitem(reader.obs._STATE, "provider", object())
    assert await prompts.version_of("reader.verify") is None  # no such template
    assert await prompts.version_of("reader.tutor") is None  # MLflow unreachable from the test: None, no raise


async def test_start_hook_never_raises(caplog: Any) -> None:
    prompts = require("reader.obs.prompts")

    class Down:
        async def enqueue(self, kind: str, args: Any, schedule_in: int = 0) -> None:
            raise RuntimeError("queue down")

    with caplog.at_level(logging.WARNING, logger="reader.obs"):
        await prompts.on_worker_start(Down())
    assert "obs prompts not enqueued" in [r.getMessage() for r in caplog.records]
