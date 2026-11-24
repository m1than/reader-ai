"""SRV-OBS [unit]: the MLflow conventions (FR-OBS-12, -13, -18) derived by AllowListExporter from validated
attributes, by VEC-OBS-4's tables, and the helpers modules call inside a job (session marker, root attributes,
child spans).

@trace FR-OBS-12 FR-OBS-13 FR-OBS-18 VEC-OBS-4 INV-OBS-1 INV-OBS-3

`oracle_derive` reads VEC-OBS-4 (spec/fixtures/SRV-OBS/analytics.yaml) independently of reader.obs.analytics and is
used by the INV-OBS-1 property test in test_allowlist.py too.
"""

from __future__ import annotations

import json
import logging
import re
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from .conftest import FIXTURES, catalogs, load_yaml, obs_setup, require

VEC4 = load_yaml(FIXTURES / "analytics.yaml")
UUID_A = "3f2c1a9e-0b4d-4c8e-9f12-6a7b8c9d0e1f"
UUID_B = "4f2c1a9e-0b4d-4c8e-9f12-6a7b8c9d0e1f"
CANARY = "bookcanary-7f3a9c"
_MICRO = Decimal("0.000001")


def _money(value: float) -> Decimal:
    return Decimal(str(value)).quantize(_MICRO, ROUND_HALF_UP)


def oracle_derive(klass: str | None, name: str, attrs: dict[str, Any], root: bool) -> dict[str, Any]:
    """VEC-OBS-4's derivations for an already-cleaned span, as parsed values (JSON keys decoded)."""
    out: dict[str, Any] = {}
    if klass is None:
        return out
    types = VEC4["span_types"]
    kind = types["job *"] if klass == "job" else types.get(klass)
    if kind:
        out["mlflow.spanType"] = kind
    if root and klass in ("job", "http", "ws"):
        out["mlflow.traceTag.mlflow.traceName"] = name
    if root and klass == "job":
        for key in VEC4["trace_tags"]:
            if key in attrs:
                out[f"mlflow.traceTag.{key}"] = str(attrs[key])
        table = VEC4["previews"].get(attrs.get("reader.job.kind"))
        if table:
            for side, key in (("inputs", "mlflow.spanInputs"), ("outputs", "mlflow.spanOutputs")):
                shown = {n: attrs[k] for n, k in table[side].items() if k in attrs}
                if shown:
                    out[key] = shown
        version, job_kind = attrs.get("reader.prompt.version"), attrs.get("reader.job.kind")
        if isinstance(version, int) and job_kind in VEC4["linked_prompts"]:
            prompt = VEC4["linked_prompts"][job_kind]
            if prompt == "reader.<reader.stage>":
                prompt = f"reader.{attrs.get('reader.stage')}"
            if prompt in VEC4["prompts"]:
                out["mlflow.traceTag.mlflow.linkedPrompts"] = [{"name": prompt, "version": str(version)}]
    cost = VEC4["llm_cost"]
    if klass == cost["span"] and isinstance(attrs.get(cost["total"]), float):
        total = _money(attrs[cost["total"]])
        given_in = attrs.get(cost["input"])
        part = min(_money(given_in), total) if isinstance(given_in, float) else Decimal(0)
        out["mlflow.llm.cost"] = {"input_cost": float(part), "output_cost": float(total - part),
                                  "total_cost": float(total)}
    if klass == VEC4["session"]["span"]:
        for key in VEC4["session"]["from"]:
            if isinstance(attrs.get(key), str):
                out["session.id"] = attrs[key]
                break
    return out


JSON_KEYS = ("mlflow.spanInputs", "mlflow.spanOutputs", "mlflow.llm.cost", "mlflow.traceTag.mlflow.linkedPrompts")


def decoded(attributes: dict[str, Any]) -> dict[str, Any]:
    """The derived keys of an exported span, JSON values parsed."""
    out = {}
    for key, value in attributes.items():
        if key.startswith("mlflow.") or key == "session.id":
            out[key] = json.loads(value) if key in JSON_KEYS else value
    return out


def test_module_equals_vec_obs_4() -> None:
    a = require("reader.obs.analytics")
    assert dict(a.SPAN_TYPES) == VEC4["span_types"]
    assert list(a.TRACE_TAGS) == VEC4["trace_tags"]
    assert {k: {s: dict(v) for s, v in t.items()} for k, t in a.PREVIEWS.items()} == VEC4["previews"]
    assert sorted(a.FORBIDDEN_PREVIEW_KEYS) == sorted(VEC4["forbidden_preview_keys"])
    assert {k: list(v) for k, v in a.JOB_ROOT_KEYS.items()} == VEC4["job_root_keys_from_handler"]
    assert dict(a.LLM_COST) == VEC4["llm_cost"]
    assert {"span": a.SESSION["span"], "from": list(a.SESSION["from"])} == VEC4["session"]
    assert dict(a.LINKED_PROMPTS) == VEC4["linked_prompts"]
    assert dict(a.PROMPTS) == VEC4["prompts"]
    for entry in VEC4["assessments"]:
        mine = a.ASSESSMENTS[entry["name"]]
        value = entry["value"] if entry["value"] == "bool" else ("float", *entry["value"]["float"])
        assert mine["value"] == value
        assert mine["source_id"] == entry["source_id"]
        assert {k: list(v) for k, v in mine["metadata"].items()} == entry["metadata"]
    assert sorted(a.ASSESSMENTS) == sorted(e["name"] for e in VEC4["assessments"])


def test_no_preview_key_is_forbidden_and_every_preview_key_is_a_root_key() -> None:
    attrs = load_yaml(FIXTURES / "attributes.yaml")
    forbidden = set(VEC4["forbidden_preview_keys"])
    for table in VEC4["previews"].values():
        for side in ("inputs", "outputs"):
            assert not forbidden & set(table[side]), table[side]
            assert set(table[side].values()) <= set(attrs["keys_by_span"]["job"])
    for keys in VEC4["job_root_keys_from_handler"].values():
        assert set(keys) <= set(attrs["keys_by_span"]["job"])
    assert set(VEC4["trace_tags"]) <= set(attrs["keys_by_span"]["job"])


def _export(name: str, attrs: dict[str, Any], parent: bool = False) -> dict[str, Any]:
    from .test_allowlist import Capture, _exporter, _span

    capture = Capture()
    span = _span(name, attrs)
    if parent:
        from opentelemetry.trace import SpanContext, TraceFlags

        span._parent = SpanContext(0x1234, 0x9999, False, TraceFlags(1))
    _exporter(capture).export([span])
    (out,) = capture.spans
    return dict(out.attributes)


def test_job_root_gets_type_name_tags_and_previews() -> None:
    out = _export("job tutor.answer", {
        "reader.job.kind": "tutor.answer", "reader.job.id": 7, "reader.question_id": UUID_A,
        "reader.lesson.id": UUID_B, "reader.job.outcome": "succeeded", "reader.tutor.state": "answered",
        "mlflow.spanType": "LLM", "session.id": CANARY, "mlflow.traceTag.reader.x": CANARY,  # caller keys: dropped
    })
    assert out["mlflow.spanType"] == "CHAIN"
    assert out["mlflow.traceTag.mlflow.traceName"] == "job tutor.answer"
    assert out["mlflow.traceTag.reader.lesson.id"] == UUID_B
    assert "mlflow.traceTag.reader.x" not in out and "session.id" not in out
    assert json.loads(out["mlflow.spanInputs"]) == {"question_id": UUID_A, "lesson_id": UUID_B}
    assert json.loads(out["mlflow.spanOutputs"]) == {"outcome": "succeeded", "tutor_state": "answered"}
    assert CANARY not in json.dumps(out)


def test_a_child_job_span_gets_no_trace_tags() -> None:
    out = _export("job tutor.answer", {"reader.job.kind": "tutor.answer"}, parent=True)
    assert out == {"reader.job.kind": "tutor.answer", "mlflow.spanType": "CHAIN"}


@pytest.mark.parametrize(("name", "kind"), [
    ("agent.call", "AGENT"), ("job.session", "CHAIN"), ("tutor.retrieve", "RETRIEVER"), ("tutor.citations", "PARSER"),
    ("verify.claims", "PARSER"), ("verify.lab", "TOOL"), ("kernel.run", "TOOL"), ("ingest.parse", "PARSER"),
    ("ingest.chunk", "PARSER"), ("embed.batch", "EMBEDDING"),
])
def test_span_types(name: str, kind: str) -> None:
    assert _export(name, {}, parent=True)["mlflow.spanType"] == kind


def test_http_root_gets_only_the_trace_name() -> None:
    out = _export("GET /api/lessons/{lesson_id}", {"http.request.method": "GET"})
    assert decoded(out) == {"mlflow.traceTag.mlflow.traceName": "GET /api/lessons/{lesson_id}"}


def test_child_span_counts_pass_their_rules() -> None:
    out = _export("verify.lab", {"reader.kernel.cells": 12, "reader.kernel.cells_ok": 20000, "reader.kernel.ms": 5},
                  parent=True)
    assert out == {"reader.kernel.cells": 12, "reader.kernel.ms": 5, "mlflow.spanType": "TOOL"}


@pytest.mark.parametrize(("total", "given_in", "expected"), [
    (0.21, 0.08292, {"input_cost": 0.08292, "output_cost": 0.12708, "total_cost": 0.21}),
    (0.1, None, {"input_cost": 0.0, "output_cost": 0.1, "total_cost": 0.1}),  # a settle with no usage
    (0.0, 0.0, {"input_cost": 0.0, "output_cost": 0.0, "total_cost": 0.0}),
    (0.3, 0.5, {"input_cost": 0.3, "output_cost": 0.0, "total_cost": 0.3}),  # an input part above the total: capped
    # a 7th-place tie rounds half up, like the ledger (code review round 1): the whole micro-dollar is input
    (0.000001, 0.0000005, {"input_cost": 0.000001, "output_cost": 0.0, "total_cost": 0.000001}),
    (0.000003, 0.0000015, {"input_cost": 0.000002, "output_cost": 0.000001, "total_cost": 0.000003}),
])
def test_llm_cost_parts_sum_to_the_total(total: float, given_in: float | None, expected: dict[str, float]) -> None:
    attrs: dict[str, Any] = {"reader.cost_usd": total}
    if given_in is not None:
        attrs["reader.cost_input_usd"] = given_in
    cost = json.loads(_export("agent.call", attrs, parent=True)["mlflow.llm.cost"])
    assert cost == expected
    assert _money(cost["input_cost"]) + _money(cost["output_cost"]) == _money(cost["total_cost"])


def test_no_settle_no_cost() -> None:
    assert "mlflow.llm.cost" not in _export("agent.call", {"reader.cost_input_usd": 0.1}, parent=True)


def test_session_from_the_marker_only() -> None:
    assert _export("job.session", {"reader.run_id": UUID_A}, parent=True)["session.id"] == UUID_A
    assert _export("job.session", {"reader.lesson.id": UUID_B}, parent=True)["session.id"] == UUID_B
    assert "session.id" not in _export("job.session", {"reader.run_id": "not-a-uuid"}, parent=True)
    assert "session.id" not in _export("agent.call", {"reader.lesson.id": UUID_B}, parent=True)


def test_linked_prompts() -> None:  # FR-OBS-18
    tutor = _export("job tutor.answer", {"reader.job.kind": "tutor.answer", "reader.prompt.version": 3})
    assert json.loads(tutor["mlflow.traceTag.mlflow.linkedPrompts"]) == [{"name": "reader.tutor", "version": "3"}]
    stage = _export("job pipeline.stage", {"reader.job.kind": "pipeline.stage", "reader.stage": "fact_check",
                                           "reader.prompt.version": 1})
    assert json.loads(stage["mlflow.traceTag.mlflow.linkedPrompts"]) == [{"name": "reader.fact_check", "version": "1"}]
    unknown = _export("job pipeline.stage", {"reader.job.kind": "pipeline.stage", "reader.stage": "verify",
                                             "reader.prompt.version": 1})
    assert "mlflow.traceTag.mlflow.linkedPrompts" not in unknown
    assert "mlflow.traceTag.mlflow.linkedPrompts" not in _export(
        "job tutor.answer", {"reader.job.kind": "tutor.answer", "reader.prompt.version": 0})


_HOSTILE = st.one_of(
    st.sampled_from(['"', "\\", "{", "}", "</script>", "\x00", " ", "x" * 10_000, CANARY,  # noqa: RUF001 (U+2028)
                     UUID_A + "x", UUID_A.upper(), f'{UUID_A}"}}']),
    st.floats(allow_nan=True, allow_infinity=True),
    st.integers(min_value=-10**12, max_value=10**12),
    st.text(max_size=50),
    st.booleans(),
)
_DERIVING = sorted({
    *VEC4["trace_tags"], "reader.cost_usd", "reader.cost_input_usd", "reader.prompt.version", "reader.tutor.state",
    *[k for t in VEC4["previews"].values() for side in t.values() for k in side.values()],
    "mlflow.spanInputs", "mlflow.llm.cost", "mlflow.spanType", "mlflow.traceTag.mlflow.traceName", "session.id",
})


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(
    name=st.sampled_from(["job tutor.answer", "job pipeline.stage", "agent.call", "job.session", "verify.lab",
                          "GET other"]),
    attrs=st.dictionaries(st.sampled_from(_DERIVING), _HOSTILE, max_size=10),
    kind=st.sampled_from(["tutor.answer", "pipeline.stage", "ingest.import", CANARY]),
)
def test_hostile_inputs_derive_only_valid_keys(name: str, attrs: dict[str, Any], kind: str) -> None:
    from .test_allowlist import oracle_clean

    attrs = {**attrs, "reader.job.kind": kind}
    out = _export(name, attrs)
    klass = require("reader.obs.attributes").AllowList(catalogs()).span_class(name)
    clean = oracle_clean(klass, attrs)
    assert decoded(out) == oracle_derive(klass, name, clean, root=True)
    for key in JSON_KEYS:
        if key in out:
            assert isinstance(json.loads(out[key]), (dict, list))
    if "mlflow.llm.cost" in out:
        c = json.loads(out["mlflow.llm.cost"])
        assert _money(c["input_cost"]) + _money(c["output_cost"]) == _money(c["total_cost"])
    assert CANARY not in json.dumps(out)
    assert all(re.fullmatch(r"[a-zA-Z0-9_.]+", k) for k in out)


# --- the helpers (reader.obs.spans) ---


def test_helpers_do_nothing_with_tracing_off(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    spans = require("reader.obs.spans")
    started: list[Any] = []
    monkeypatch.setattr(threading.Thread, "start", lambda self: started.append(self))
    with obs_setup(enabled=False):
        spans.session_marker("reader.run_id", UUID_A)
        spans.annotate_job({"reader.lesson.id": UUID_B})
        with spans.child_span("tutor.retrieve", {"reader.retrieval.hits": 1}) as child:
            child.set({"reader.retrieval.candidates": 2})
    assert started == []


def test_session_marker_is_a_flushed_child(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    spans = require("reader.obs.spans")
    threads: list[Any] = []
    real_start = threading.Thread.start
    monkeypatch.setattr(threading.Thread, "start", lambda self: (threads.append(self), real_start(self)))
    with obs_setup(role="worker") as (handle, exported):
        tracer = require("reader.obs").tracer()
        with tracer.start_as_current_span("job pipeline.stage", attributes={"reader.job.kind": "pipeline.stage"}):
            spans.session_marker("reader.run_id", UUID_A)
        for t in threads:
            t.join(5)
        handle.force_flush()
        got = {s.name: s for s in exported.get_finished_spans()}
    assert [t.daemon for t in threads if t.name == "obs-session-flush"] == [True]
    marker = got["job.session"]
    assert marker.attributes["session.id"] == UUID_A
    assert marker.attributes["mlflow.spanType"] == "CHAIN"
    assert marker.parent is not None and marker.parent.span_id == got["job pipeline.stage"].context.span_id


def test_session_marker_failure_is_logged_once(monkeypatch: pytest.MonkeyPatch, caplog: Any) -> None:
    import threading

    spans = require("reader.obs.spans")

    def boom(self: Any) -> None:
        raise RuntimeError("no thread")

    monkeypatch.setattr(spans, "_LOGGED", set())
    with obs_setup(role="worker"), caplog.at_level(logging.WARNING, logger="reader.obs"):
        monkeypatch.setattr(threading.Thread, "start", boom)  # after setup: its own threads have started
        spans.session_marker("reader.run_id", UUID_A)
        spans.session_marker("reader.run_id", UUID_A)
        monkeypatch.undo()
    assert [r.getMessage() for r in caplog.records].count("obs session_marker_failed") == 1


def test_a_failing_flush_thread_is_logged_once(monkeypatch: pytest.MonkeyPatch, caplog: Any) -> None:
    """FR-OBS-12 (e): an error inside the daemon flush thread is caught there and logged once; the job's own
    task never sees it (INV-OBS-3)."""
    spans = require("reader.obs.spans")

    class Broken:
        def force_flush(self, timeout_millis: int) -> bool:
            raise RuntimeError(CANARY)

    monkeypatch.setattr(spans, "_LOGGED", set())
    with caplog.at_level(logging.WARNING, logger="reader.obs"):
        spans._flush(Broken())
        spans._flush(Broken())
    lines = [r.getMessage() for r in caplog.records]
    assert lines.count("obs session_marker_failed") == 1
    assert not any(CANARY in line for line in lines)


def test_annotate_job_never_raises(monkeypatch: pytest.MonkeyPatch, caplog: Any) -> None:
    """INV-OBS-3: a failure while setting root keys is logged once and never reaches the job."""
    spans = require("reader.obs.spans")

    def boom(span: Any, attributes: Any) -> None:
        raise RuntimeError(CANARY)

    monkeypatch.setattr(spans, "_LOGGED", set())
    monkeypatch.setattr(spans, "_set", boom)
    with obs_setup(role="worker"), caplog.at_level(logging.WARNING, logger="reader.obs"):
        tracer = require("reader.obs").tracer()
        with tracer.start_as_current_span("job tutor.answer"):
            spans.annotate_job({"reader.lesson.id": UUID_B})
            spans.annotate_job({"reader.lesson.id": UUID_B})
    lines = [r.getMessage() for r in caplog.records]
    assert lines.count("obs annotate_job_failed") == 1


def test_child_span_status_and_annotate(monkeypatch: pytest.MonkeyPatch) -> None:
    spans = require("reader.obs.spans")
    from opentelemetry.trace import StatusCode

    with obs_setup(role="worker") as (handle, exported):
        tracer = require("reader.obs").tracer()
        with tracer.start_as_current_span("job tutor.answer", attributes={"reader.job.kind": "tutor.answer"}):
            spans.annotate_job({"reader.lesson.id": UUID_B, "reader.course.id": None})
            with spans.child_span("tutor.retrieve") as ok:
                ok.set({"reader.retrieval.hits": 3})
            with pytest.raises(ValueError), spans.child_span("tutor.citations"):
                raise ValueError(CANARY)
        handle.force_flush()
        got = {s.name: s for s in exported.get_finished_spans()}
    assert got["tutor.retrieve"].status.status_code == StatusCode.OK
    assert got["tutor.retrieve"].attributes["reader.retrieval.hits"] == 3
    assert got["tutor.citations"].status.status_code == StatusCode.ERROR
    assert got["tutor.citations"].status.description is None
    assert got["job tutor.answer"].attributes["reader.lesson.id"] == UUID_B
    assert "reader.course.id" not in got["job tutor.answer"].attributes


# --- the agent span (FR-OBS-12 b, c) ---


def _usage(i: int, o: int, w5: int, w1: int, r: int, searches: int | None = None) -> Any:
    return SimpleNamespace(input=i, output=o, cache_write_5m=w5, cache_write_1h=w1, cache_read=r,
                           prompt_tokens=i + w5 + w1 + r, web_search_requests=searches)


def test_input_part_uses_each_cache_rate() -> None:
    agent = require("reader.obs.agent")
    rates = {"input": Decimal(2), "output": Decimal(10), "cache_write_5m": Decimal("2.5"),
             "cache_write_1h": Decimal(4), "cache_read": Decimal("0.1")}
    usage = _usage(50, 4000, 6000, 2000, 30000)
    priced_in = Decimal(50 * 2) + Decimal(30000) * Decimal("0.1") + Decimal(6000) * Decimal("2.5") + Decimal(2000 * 4)
    priced_out = Decimal(4000 * 10)
    want = (Decimal("0.21") * priced_in / (priced_in + priced_out)).quantize(_MICRO, ROUND_HALF_UP)
    assert agent.input_part(Decimal("0.21"), usage, rates) == want
    assert agent.input_part(Decimal("0.1"), _usage(0, 0, 0, 0, 0), rates) == Decimal("0.000000")
    assert agent.input_part(Decimal("0"), usage, rates) == Decimal("0")
    # The search fee counts on the output side (rates are per million tokens, the fee in USD).
    fee = Decimal("0.02")
    with_fee = (Decimal("0.21") * priced_in / (priced_in + priced_out + fee * 10**6)).quantize(_MICRO, ROUND_HALF_UP)
    assert agent.input_part(Decimal("0.21"), usage, rates, fee) == with_fee < want


def test_input_part_rounds_half_up_like_the_ledger() -> None:
    """A tie at the 7th place: half up gives the larger micro-dollar, half even would keep the even one."""
    agent = require("reader.obs.agent")
    rates = {"input": Decimal(1), "output": Decimal(1), "cache_write_5m": Decimal(1),
             "cache_write_1h": Decimal(1), "cache_read": Decimal(1)}
    # settled 0.000005, half input: 0.0000025 exactly
    assert agent.input_part(Decimal("0.000005"), _usage(1, 1, 0, 0, 0), rates) == Decimal("0.000003")


def test_priced_input_part_uses_the_tier_and_the_fee() -> None:
    """FR-AGENT-17's tier (haiku above 100k prompt tokens) and the call's search fee (pricing.yaml)."""
    agent = require("reader.obs.agent")
    from reader.agent.pricing import load_pricing, rates

    big = _usage(150_000, 2000, 0, 0, 0)
    high, low = rates("claude-haiku-5-5", 150_000), rates("claude-haiku-5-5", 0)
    settled = Decimal("0.08")
    assert agent.priced_input_part("claude-haiku-5-5", settled, big) == agent.input_part(settled, big, high)
    assert agent.priced_input_part("claude-haiku-5-5", settled, big, summed=True) == agent.input_part(
        settled, big, low)
    searched = _usage(5000, 800, 0, 0, 20000, searches=2)
    fee = 2 * load_pricing().web_search_usd
    son = rates("claude-sonnet-5-5")
    assert agent.priced_input_part("claude-sonnet-5-5", settled, searched) == agent.input_part(
        settled, searched, son, fee)
    assert fee > 0
    # Code review round 2: both at once, the fee and a prompt above 100k on a tiered model (not summed).
    both = _usage(150_000, 2000, 0, 0, 0, searches=3)
    fee3 = 3 * load_pricing().web_search_usd
    assert agent.priced_input_part("claude-haiku-5-5", settled, both) == agent.input_part(settled, both, high, fee3)
    assert agent.input_part(settled, both, high, fee3) < agent.input_part(settled, both, high)


def test_agent_span_semconv_keys_and_cost() -> None:
    agent = require("reader.obs.agent")
    with obs_setup(role="worker") as (handle, exported):
        with agent.agent_call("fact_check", "fact_check", "claude-sonnet-5-5", lesson_id=None, step=2) as span:
            span.settled(_usage(50, 4000, 6000, 2000, 30000), Decimal("0.21"), "end_turn")
        handle.force_flush()
        (out,) = exported.get_finished_spans()
    a = dict(out.attributes)
    assert a["gen_ai.usage.input_tokens"] == 50 + 6000 + 2000 + 30000
    assert a["gen_ai.usage.cache_read.input_tokens"] == 30000
    assert a["gen_ai.usage.cache_write.input_tokens"] == 8000
    assert a["gen_ai.operation.name"] == "invoke_agent"
    assert a["gen_ai.provider.name"] == "anthropic"
    assert a["reader.step"] == 2
    assert a["mlflow.spanType"] == "AGENT"
    cost = json.loads(a["mlflow.llm.cost"])
    assert cost["total_cost"] == 0.21
    assert 0 < cost["input_cost"] < 0.21
    assert _money(cost["input_cost"]) + _money(cost["output_cost"]) == _money(cost["total_cost"])
    assert "gen_ai.usage.cache_read_input_tokens" not in a


@pytest.mark.parametrize(("usage", "settled", "parts"), [
    # A tie (code review round 1): one 5m cache-write token on sonnet is 2.5e-6 USD; the ledger's micro() rounds
    # it half up to 0.000003, which is all input: the parts are exact at 6 places.
    (_usage(0, 0, 1, 0, 0), Decimal("0.000003"), (0.000003, 0.0, 0.000003)),
    # A settle with no usage (a failed call billed at its reserve): the whole amount counts as output.
    (None, Decimal("0.25"), (0.0, 0.25, 0.25)),
])
def test_agent_span_cost_edges(usage: Any, settled: Decimal, parts: tuple[float, float, float]) -> None:
    agent = require("reader.obs.agent")
    with obs_setup(role="worker") as (handle, exported):
        with agent.agent_call("fact_check", "fact_check", "claude-sonnet-5-5") as span:
            span.settled(usage, settled, None)
        handle.force_flush()
        (out,) = exported.get_finished_spans()
    a = dict(out.attributes)
    assert a["reader.cost_usd"] == float(settled)
    assert a["reader.cost_input_usd"] == parts[0]
    assert json.loads(a["mlflow.llm.cost"]) == dict(zip(("input_cost", "output_cost", "total_cost"), parts, strict=True))
    if usage is None:
        assert "gen_ai.usage.input_tokens" not in a
