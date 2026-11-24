"""SRV-OBS [unit]: derived job trace ids, traceparent parsing, the job root span and its link.

@trace FR-OBS-5 INV-OBS-4
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from .conftest import finished, obs_setup, require


def _expected(job_id: int, key: str) -> int:
    digest = hashlib.sha256(b"reader.job\x00" + str(job_id).encode("ascii") + b"\x00" + key.encode()).digest()
    first = digest[:16]
    return int.from_bytes(first if any(first) else digest[16:32], "big")


@given(job_id=st.integers(min_value=1, max_value=2**63 - 1), key=st.text(min_size=1, max_size=80))
def test_derive_trace_id(job_id: int, key: str) -> None:
    ids = require("reader.obs.ids")
    assert ids.derive_trace_id(job_id, key) == _expected(job_id, key)
    assert ids.derive_trace_id(job_id, key) != 0


def test_derive_trace_id_known_value() -> None:
    ids = require("reader.obs.ids")
    key = "pipeline.stage:742e7805a30140378d3cd6884489c645c4827e17fb70c17ae18b402077fa4d95"
    assert format(ids.derive_trace_id(106, key), "032x") == "85b524475f9f651f8b30426af5fbb218"


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "garbage",
        "00-" + "0" * 32 + "-" + "1" * 16 + "-01",
        "00-" + "1" * 32 + "-" + "0" * 16 + "-01",
        "01-" + "1" * 32 + "-" + "1" * 16 + "-01",
        "00-" + "1" * 31 + "-" + "1" * 16 + "-01",
        "00-" + "g" * 32 + "-" + "1" * 16 + "-01",
        "00-" + "1" * 32 + "-" + "1" * 16 + "-00",
        "00-" + "AB" * 16 + "-" + "cd" * 8 + "-01",  # W3C: lower-case hex only (round 2)
        "00-" + "ab" * 16 + "-" + "CD" * 8 + "-01",
        "00-" + "ab" * 16 + "-" + "cd" * 8 + "-0B",
    ],
)
def test_parse_traceparent_refuses(value: str | None) -> None:
    assert require("reader.obs.propagation").parse_traceparent(value) is None


def test_job_span_inside_an_active_span_starts_a_new_trace() -> None:
    """Round 1 survivor: opened under a current span (and no `trace` kwarg), the root has no parent, the derived
    trace id, and no link: a job never joins the trace that happens to be current in the worker."""
    prop = require("reader.obs.propagation")
    ids = require("reader.obs.ids")
    key = "tutor.answer:" + "a" * 64
    tracer = require("reader.obs").tracer
    with obs_setup("worker") as (handle, memory):
        with tracer().start_as_current_span("POST /api/lessons/{lesson_id}"), prop.job_span("tutor.answer", 42, key, {}) as job:
            job.outcome("succeeded")
        spans = finished(handle, memory)
    (root,) = [s for s in spans if s.name == "job tutor.answer"]
    assert root.parent is None
    assert root.context.trace_id == ids.derive_trace_id(42, key)
    assert list(root.links) == []
    assert format(root.context.trace_flags, "02x") == "01"


def test_parse_traceparent_accepts() -> None:
    ctx = require("reader.obs.propagation").parse_traceparent("00-" + "ab" * 16 + "-" + "cd" * 8 + "-01")
    assert ctx is not None
    assert format(ctx.trace_id, "032x") == "ab" * 16
    assert format(ctx.span_id, "016x") == "cd" * 8


def _job_span(**kw: Any) -> Any:
    prop = require("reader.obs.propagation")
    args = {
        "kind": "tutor.answer",
        "job_id": 41,
        "key": "tutor.answer:" + "a" * 64,
        "payload": {"question_id": "30000000-0000-4000-8000-000000000001", "secret": "x"},
        "trace": None,
    }
    args.update(kw)
    return prop.job_span(**args)


def test_job_span_links_to_the_enqueuer_in_a_new_derived_trace() -> None:
    prop = require("reader.obs.propagation")
    ids = require("reader.obs.ids")
    with obs_setup("worker") as (handle, memory):
        with require("reader.obs").tracer().start_as_current_span("POST /api/lessons/{lesson_id}"):
            enqueuer = prop.current_traceparent()
        with _job_span(trace=enqueuer) as job:
            job.attempt(2)
            inner = prop.current_traceparent()
            job.outcome("succeeded")
        spans = {s.name: s for s in finished(handle, memory)}
    root = spans["job tutor.answer"]
    assert root.parent is None
    assert root.context.trace_id == ids.derive_trace_id(41, "tutor.answer:" + "a" * 64)
    assert [format(link.context.trace_id, "032x") for link in root.links] == [enqueuer.split("-")[1]]
    assert [format(link.context.span_id, "016x") for link in root.links] == [enqueuer.split("-")[2]]
    assert root.kind.name == "CONSUMER"
    assert dict(root.attributes) == {
        "reader.job.kind": "tutor.answer",
        "reader.job.id": 41,
        "reader.job.attempt": 2,
        "reader.question_id": "30000000-0000-4000-8000-000000000001",
        "reader.job.outcome": "succeeded",
    }
    assert inner is not None and inner.split("-")[1] == format(root.context.trace_id, "032x")


@pytest.mark.parametrize(
    "trace",
    [
        None,
        "not-a-traceparent",
        "00-" + "0" * 32 + "-" + "1" * 16 + "-01",
        "00-" + "1" * 32 + "-" + "1" * 16 + "-00",
    ],
)
def test_job_span_without_a_valid_trace_has_no_link(trace: str | None) -> None:
    with obs_setup("worker") as (handle, memory):
        with _job_span(trace=trace):
            pass
        (root,) = finished(handle, memory)
    assert list(root.links) == []


def test_job_span_outcome_and_codes() -> None:
    with obs_setup("worker") as (handle, memory):
        with _job_span(job_id=1) as job:
            job.outcome("dead", "provider_unavailable")
        with _job_span(job_id=2) as job:
            job.outcome("failed", "ValueError")
        with _job_span(job_id=3) as job:
            job.outcome("failed", "Book text as a code")  # neither a catalogue code nor a class name
        spans = sorted(finished(handle, memory), key=lambda s: s.attributes["reader.job.id"])
    assert spans[0].attributes["reader.error_code"] == "provider_unavailable"
    assert spans[0].attributes["reader.job.outcome"] == "dead"
    assert spans[1].attributes["reader.error_class"] == "ValueError"
    assert "reader.error_code" not in spans[2].attributes and "reader.error_class" not in spans[2].attributes


def test_job_args_are_the_declared_fields_only() -> None:
    with obs_setup("worker") as (handle, memory):
        with _job_span(
            kind="pipeline.stage",
            key="pipeline.stage:" + "b" * 64,
            payload={
                "run_id": "10000000-0000-4000-8000-000000000001",
                "step": 2,
                "stage": "fact_check",
                "input_sha256": "2" * 64,
            },
        ):
            pass
        (root,) = finished(handle, memory)
    attrs = dict(root.attributes)
    assert attrs["reader.run_id"] == "10000000-0000-4000-8000-000000000001"
    assert attrs["reader.step"] == 2
    assert attrs["reader.stage"] == "fact_check"
    assert "input_sha256" not in repr(attrs)


def test_other_spans_get_random_ids_and_helpers_never_raise() -> None:
    ids = require("reader.obs.ids")
    with obs_setup("worker") as (handle, memory):
        with _job_span() as job, require("reader.obs").tracer().start_as_current_span("agent.call"):
            job.outcome("not-an-outcome")  # ignored, never raises
        spans = {s.name: s for s in finished(handle, memory)}
    assert spans["agent.call"].context.trace_id == spans["job tutor.answer"].context.trace_id
    assert spans["agent.call"].context.span_id != ids.derive_trace_id(41, "tutor.answer:" + "a" * 64) & (
        2**64 - 1
    )


def test_disabled_job_span_is_a_noop() -> None:
    prop = require("reader.obs.propagation")
    setup = require("reader.obs.setup")
    from .conftest import catalogs, config

    handle = setup.setup("worker", config(enabled=False), catalogs=catalogs())
    try:
        with _job_span(trace="00-" + "1" * 32 + "-" + "1" * 16 + "-01") as job:
            job.attempt(1)
            job.outcome("succeeded")
            assert prop.current_traceparent() is None
    finally:
        handle.shutdown()
