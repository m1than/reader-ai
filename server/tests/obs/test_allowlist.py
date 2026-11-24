"""SRV-OBS [unit]: the declared allow-list and the exporter that rebuilds every span against it.

@trace FR-OBS-2 INV-OBS-1 VEC-OBS-1
"""

from __future__ import annotations

import re
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from .conftest import FIXTURES, catalogs, load_yaml, require

VEC = load_yaml(FIXTURES / "attributes.yaml")
CANARY = "bookcanary-7f3a9c"


def _norm(rule: dict[str, Any]) -> dict[str, Any]:
    return {k: (list(v) if isinstance(v, (tuple, list, frozenset, set)) else v) for k, v in rule.items()}


def test_module_data_equals_vec_obs_1() -> None:
    attrs = require("reader.obs.attributes")
    assert {k: _norm(v) for k, v in attrs.ATTRIBUTES.items()} == VEC["attributes"]
    assert {k: list(v) for k, v in attrs.KEYS_BY_SPAN.items()} == VEC["keys_by_span"]
    assert {k: dict(v) for k, v in attrs.JOB_ARGS.items()} == VEC["job_args"]
    assert attrs.FALLBACK_NAME == VEC["fallback_name"] == "reader.unlisted"
    assert attrs.SCOPE == VEC["instrumentation_scope"]["name"] == "reader"


JOB_CODES = {
    "exhausted",
    "stalled",
    "orphaned",
    "timeout",
    "session_lost",
    "wrapper_error",
    "unknown_kind",
    "no_key_row",
}


def _declared_error_codes() -> frozenset[str]:
    specs = FIXTURES.parent.parent / "modules"
    declared: set[str] = set()
    for name in ("SRV-AGENT", "SRV-TUTOR", "SRV-PIPELINE"):
        declared |= set(load_yaml(specs / f"{name}.yaml")["errors"])
    return frozenset(declared | JOB_CODES)


def test_error_codes_are_the_declared_catalogues() -> None:
    assert _declared_error_codes() == require("reader.obs.attributes").ERROR_CODES


def _oracle_enums() -> dict[str, frozenset[str]]:
    """The `enum_from` sets as VEC-OBS-1 defines them, from the test catalogs and the specs, not the module."""
    cat = catalogs()
    return {
        "route_templates": frozenset(cat.route_templates) | {"other"},
        "job_kinds": frozenset(cat.job_kinds),
        "pipeline_stages": frozenset(cat.pipeline_stages),
        "tasks": frozenset(cat.tasks),
        "models": frozenset(cat.models),
        "error_codes": _declared_error_codes(),
    }


def oracle_valid(key: str, value: object, enums: dict[str, frozenset[str]]) -> bool:
    """VEC-OBS-1's value rule for `key`, read from the YAML (independent of reader.obs.attributes)."""
    rule = VEC["attributes"].get(key)
    if rule is None:
        return False
    if rule["type"] == "string":
        if type(value) is not str or len(value) > 128:
            return False
        if "enum" in rule:
            return value in rule["enum"]
        if "enum_from" in rule:
            return value in enums[rule["enum_from"]]
        return re.fullmatch(rule["pattern"], value) is not None
    wanted = {"int": int, "float": float}[rule["type"]]
    if type(value) is not wanted:  # a bool is neither an int nor a float here
        return False
    low, high = rule["range"]
    return bool(low <= value <= high)  # type: ignore[operator]


def oracle_clean(span_class: str | None, attrs: dict[str, Any]) -> dict[str, Any]:
    enums = _oracle_enums()
    allowed = VEC["keys_by_span"].get(span_class or "", [])
    return {k: v for k, v in attrs.items() if k in allowed and oracle_valid(k, v, enums)}


UUID = "3f2c1a9e-0b4d-4c8e-9f12-6a7b8c9d0e1f"
SHA = "ab" * 32
ROWS = [
    # key, value, valid (code review round 1 survivors first)
    ("reader.run_id", f"{UUID} text", False),
    ("reader.run_id", f"{UUID}\n", False),
    ("reader.run_id", UUID.upper(), False),
    ("reader.run_id", UUID, True),
    ("reader.material_sha256", SHA + "0" * 100, False),
    ("reader.material_sha256", SHA, True),
    ("reader.material_sha256", SHA[:-1], False),
    ("http.response.status_code", 99999, False),
    ("http.response.status_code", 99, False),
    ("http.response.status_code", 100, True),
    ("http.response.status_code", 599, True),
    ("http.response.status_code", 600, False),
    ("reader.step", -1, False),
    ("reader.step", 0, True),
    ("reader.step", 999, True),
    ("reader.step", 1000, False),
    ("reader.job.id", True, False),
    ("reader.job.id", 0, False),
    ("reader.job.id", 1, True),
    ("reader.job.id", 9223372036854775807, True),
    ("reader.job.id", 9223372036854775808, False),
    ("reader.job.attempt", 1.0, False),
    ("reader.ws.close_code", 999, False),
    ("reader.ws.close_code", 4999, True),
    ("reader.cost_usd", 1, False),
    ("reader.cost_usd", -0.01, False),
    ("reader.cost_usd", 1000.0, True),
    ("reader.cost_usd", 1000.5, False),
    ("gen_ai.usage.input_tokens", 1_000_000_001, False),
    ("gen_ai.usage.input_tokens", False, False),
    ("reader.error_class", "A", False),
    ("reader.error_class", "A" + "b" * 63, True),
    ("reader.error_class", "A" + "b" * 64, False),
    ("reader.error_class", "valueError", False),
    ("reader.error_code", "Transient", False),
    ("reader.error_code", "provider_unavailable", True),
    ("reader.job.outcome", "dead", True),
    ("reader.job.outcome", "Dead", False),
    ("http.request.method", "get", False),
    ("http.route", "other", True),
    ("reader.stop_reason", "end_turn ", False),
]


def test_oracle_rows() -> None:
    allow = require("reader.obs.attributes").AllowList(catalogs())
    enums = _oracle_enums()
    for key, value, valid in ROWS:
        assert oracle_valid(key, value, enums) is valid, (key, value)
        assert allow.valid(key, value) is valid, (key, value)


def test_module_agrees_with_the_oracle_on_every_key() -> None:
    allow = require("reader.obs.attributes").AllowList(catalogs())
    enums = _oracle_enums()
    samples: list[object] = [None, True, 0, 1, -1, 1.5, "", "x" * 129, UUID, SHA, "GET", "other", "dead"]
    samples += sorted({v for values in enums.values() for v in values})
    for key in VEC["attributes"]:
        for value in samples:
            assert allow.valid(key, value) is oracle_valid(key, value, enums), (key, value)


def test_span_names_by_class() -> None:
    allow = require("reader.obs.attributes").AllowList(catalogs())
    assert allow.span_class("GET /api/lessons/{lesson_id}") == "http"
    assert allow.span_class("GET other") == "http"
    assert allow.span_class("WS /api/kernel/sessions/{session_id}/channels") == "ws"
    assert allow.span_class("job tutor.answer") == "job"
    assert allow.span_class("agent.call") == "agent.call"
    # Raw paths, unknown kinds and free text are not span names.
    for bad in ("GET /api/lessons/3f2c", "job unknown.kind", "reader.stage draft", CANARY, "GET /api/x"):
        assert allow.span_class(bad) is None, bad


def test_clean_keeps_only_listed_valid_values() -> None:
    allow = require("reader.obs.attributes").AllowList(catalogs())
    kept = allow.clean(
        "http",
        {
            "http.request.method": "GET",
            "http.route": "/api/lessons/{lesson_id}",
            "http.response.status_code": 200,
            "url.path": "/api/lessons/3f2c",  # unlisted key
            "reader.job.kind": "tutor.answer",  # listed, but not for http spans
        },
    )
    assert kept == {
        "http.request.method": "GET",
        "http.route": "/api/lessons/{lesson_id}",
        "http.response.status_code": 200,
    }
    job = allow.clean(
        "job",
        {
            "reader.job.kind": "tutor.answer",
            "reader.job.id": 7,
            "reader.job.attempt": "1",  # wrong type
            "reader.error_code": "provider_unavailable",
            "reader.error_class": "ValueError",
            "reader.stage": "not_a_stage",  # not in the catalog
            "reader.material_sha256": "z" * 64,  # fails its pattern
            "reader.question_id": CANARY,
        },
    )
    assert job == {
        "reader.job.kind": "tutor.answer",
        "reader.job.id": 7,
        "reader.error_code": "provider_unavailable",
        "reader.error_class": "ValueError",
    }
    assert allow.clean("job", {"reader.error_code": "Book text here"}) == {}
    assert allow.clean(
        "agent.call",
        {
            "reader.cost_usd": 0.12,
            "gen_ai.request.model": "claude-sonnet-5-5",
            "reader.task": "tutor",
            "reader.stop_reason": "end_turn",
        },
    ) == {
        "reader.cost_usd": 0.12,
        "gen_ai.request.model": "claude-sonnet-5-5",
        "reader.task": "tutor",
        "reader.stop_reason": "end_turn",
    }
    assert allow.clean("agent.call", {"reader.cost_usd": True}) == {}  # a bool is not a float
    assert allow.clean("http", {"http.route": "x" * 200}) == {}


def _span(
    name: str,
    attrs: dict[str, Any],
    *,
    events: list[tuple[str, dict[str, Any]]] = (),  # type: ignore[assignment]
    description: str | None = None,
    link_attrs: dict[str, Any] | None = None,
    tracestate: str = "",
) -> Any:
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import Event, ReadableSpan
    from opentelemetry.sdk.util.instrumentation import InstrumentationScope
    from opentelemetry.trace import Link, SpanContext, SpanKind, Status, StatusCode, TraceFlags, TraceState

    state = TraceState.from_header([tracestate]) if tracestate else TraceState()
    ctx = SpanContext(0x1234, 0x5678, False, TraceFlags(1), state)
    link = Link(SpanContext(0x99, 0x98, False, TraceFlags(1), state), link_attrs or {})
    return ReadableSpan(
        name=name,
        context=ctx,
        parent=None,
        resource=Resource({"service.name": CANARY, "host.name": CANARY}),
        attributes=attrs,
        events=[Event(n, a) for n, a in events],
        links=[link],
        kind=SpanKind.INTERNAL,
        status=Status(StatusCode.ERROR, description) if description else Status(StatusCode.UNSET),
        start_time=1,
        end_time=2,
        instrumentation_scope=InstrumentationScope(CANARY, "9"),
    )


class Capture:
    def __init__(self) -> None:
        self.spans: list[Any] = []

    def export(self, spans: Any) -> Any:
        from opentelemetry.sdk.trace.export import SpanExportResult

        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None: ...

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def _exporter(capture: Capture) -> Any:
    export = require("reader.obs.export")
    attrs = require("reader.obs.attributes")
    return export.AllowListExporter(capture, attrs.AllowList(catalogs()), export.resource("api"))


def _check_clean(out: Any, allowed: Any, given: dict[str, Any] | None = None) -> None:
    name_ok = out.name == "reader.unlisted" or allowed.span_class(out.name) is not None
    assert name_ok, out.name
    klass = allowed.span_class(out.name)
    source = dict(out.attributes) if given is None else given
    # The oracle (VEC-OBS-1 read independently), applied to what went in: exactly the valid listed keys survive.
    assert dict(out.attributes) == (oracle_clean(klass, source) if klass else {})
    assert list(out.events) == []
    assert out.status.description is None
    assert all(dict(link.attributes or {}) == {} for link in out.links)
    assert all(len(link.context.trace_state) == 0 for link in out.links)
    assert len(out.context.trace_state) == 0
    assert out.instrumentation_scope.name == "reader"
    assert out.instrumentation_scope.version is None
    assert set(out.resource.attributes) == {"service.name", "service.version"}
    assert out.resource.attributes["service.name"] == "reader-api"
    assert CANARY not in repr(out.to_json())


def test_exporter_rebuilds_every_part() -> None:
    capture = Capture()
    _exporter(capture).export(
        [
            _span(
                "GET /api/lessons/3f2c",
                {"url.path": CANARY},
                events=[("exception", {"exception.message": CANARY})],
                description=CANARY,
                link_attrs={"note": CANARY},
                tracestate=f"k={CANARY}",
            ),
            _span("job tutor.answer", {"reader.job.kind": "tutor.answer", "reader.question_id": CANARY}),
        ]
    )
    first, second = capture.spans
    assert first.name == "reader.unlisted"
    assert dict(first.attributes) == {}
    assert second.name == "job tutor.answer"
    assert dict(second.attributes) == {"reader.job.kind": "tutor.answer"}
    allowed = require("reader.obs.attributes").AllowList(catalogs())
    for out in capture.spans:
        _check_clean(out, allowed)


def test_exporter_never_raises() -> None:
    from opentelemetry.sdk.trace.export import SpanExportResult

    class Boom(Capture):
        def export(self, spans: Any) -> Any:
            raise RuntimeError(CANARY)

    result = _exporter(Boom()).export([_span("agent.call", {})])
    assert result == SpanExportResult.FAILURE
    assert _exporter(Capture()).export([object()]) == SpanExportResult.FAILURE  # not a span at all


def test_dropped_key_logged_once_by_name(caplog: Any) -> None:
    import logging

    exporter = _exporter(Capture())
    with caplog.at_level(logging.WARNING, logger="reader.obs"):
        for _ in range(3):
            exporter.export([_span("agent.call", {"prompt": CANARY, "Bad Key!": CANARY})])
    lines = [r.getMessage() for r in caplog.records]
    assert lines.count("obs dropped key=prompt") == 1
    assert lines.count("obs dropped key=<invalid>") == 1
    assert all(CANARY not in line for line in lines)


_VALUE = st.one_of(
    st.text(max_size=40),
    st.integers(),
    st.floats(allow_nan=False),
    st.booleans(),
    st.sampled_from([CANARY, "GET", "tutor.answer", "/api/lessons/{lesson_id}", "end_turn"]),
)
_KEY = st.one_of(st.sampled_from(sorted(VEC["attributes"])), st.text(min_size=1, max_size=30))
_NAME = st.one_of(
    st.sampled_from(
        [
            "GET /api/lessons/{lesson_id}",
            "job tutor.answer",
            "agent.call",
            "WS /api/kernel/sessions/{session_id}/channels",
            "GET other",
        ]
    ),
    st.text(max_size=40),
    st.just(CANARY),
)


@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
@given(
    name=_NAME,
    attrs=st.dictionaries(_KEY, _VALUE, max_size=8),
    desc=st.one_of(st.none(), st.text(max_size=20)),
    link_attrs=st.dictionaries(st.text(min_size=1, max_size=10), st.text(max_size=10), max_size=3),
)
def test_inv_obs_1_property(
    name: str, attrs: dict[str, Any], desc: str | None, link_attrs: dict[str, Any]
) -> None:
    capture = Capture()
    _exporter(capture).export(
        [
            _span(
                name,
                attrs,
                events=[("e", {"x": CANARY})],
                description=desc or None,
                link_attrs=link_attrs,
                tracestate=f"k={re.sub(r'[^a-z]', 'a', 'v')}",
            )
        ]
    )
    (out,) = capture.spans
    allowed = require("reader.obs.attributes").AllowList(catalogs())
    _check_clean(out, allowed, attrs)
