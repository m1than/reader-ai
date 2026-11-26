"""SRV-OBS [unit]: the text-free `find` subtree (FR-OBS-36) and the pack-build and link-run spans (FR-OBS-37), opened
through the reader.obs helpers and exported through the real AllowListExporter, against VEC-OBS-7
(spec/fixtures/SRV-OBS/find-spans.yaml).

@trace FR-OBS-36 FR-OBS-37 VEC-OBS-7 INV-OBS-1

The [image] half of FR-OBS-36 and FR-OBS-37 (test_images_kl.py: ROUTER stored by MLflow 3.18.0, the judge.find
link, the measured size, the no-double-count cost sum) is not written here.
"""

from __future__ import annotations

import hashlib
import json
import re
import typing
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from .conftest import FIXTURES, load_yaml, obs_setup, require

VEC7 = load_yaml(FIXTURES / "find-spans.yaml")
VEC1 = load_yaml(FIXTURES / "attributes.yaml")
VEC4 = load_yaml(FIXTURES / "analytics.yaml")
FAMILY = VEC1["key_families"]["reader.doc.<i>"]
URI = re.compile(FAMILY["keys"]["uri"]["pattern"])
CLEAR = re.compile(URI.pattern.replace("^reader://", "^").replace("|h/[0-9a-f]{64}", ""))
CAP = FAMILY["i"][1] + 1  # 25
UUID_A = "3f2c1a9e-0b4d-4c8e-9f12-6a7b8c9d0e1f"


def cats() -> dict[str, Any]:
    c = VEC7["catalogs"]
    return {
        "find_routes": frozenset(c["find_routes"]),
        "facet_values": {k: frozenset(v) for k, v in c["facet_values"].items()},
        "topic_paths": frozenset(c["topic_paths"]),
    }


@contextmanager
def tracing() -> Iterator[tuple[Any, Any]]:
    with obs_setup(role="worker", **cats()) as (handle, memory):
        yield handle, memory


def _docs(find: Any, select: bool) -> list[Any]:
    return [find.doc(c["id"], c["sha256"], c["tokens"], c["judged"] if select else None) for c in VEC7["cards"]]


def _done_args(find: Any, name: str, done: dict[str, Any]) -> dict[str, Any]:
    args = dict(done)
    if args.pop("documents", None) == "cards":
        args["docs"] = _docs(find, select=name == "find.select")
    if "judge" in args:
        args["judge"] = find.Judge(**args["judge"])
    return args


def _openers(find: Any) -> dict[str, Any]:
    return {"find.route": find.route, "find.retrieve": find.retrieve, "find.preselect": find.preselect,
            "find.judge_wait": find.judge_wait, "find.select": find.select}


def run_vec7(find: Any) -> None:
    """The recorded find, driven through the helpers exactly as the fixture states it."""
    root, *children = VEC7["find"]
    with find.find(**root["open"]) as top:
        for entry in children:
            with _openers(find)[entry["name"]](**entry["open"]) as part:
                part.done(**_done_args(find, entry["name"], entry["done"]))
        top.done(**_done_args(find, "find", root["done"]))


def expected_docs(name: str) -> dict[str, Any]:
    keys = FAMILY["spans"].get(name)
    if not keys:
        return {}
    out: dict[str, Any] = {}
    shown = VEC7["cards"][:CAP]
    for i, card in enumerate(shown):
        for key in keys:
            out[f"reader.doc.{i}.{key}"] = card[key]
    out["mlflow.spanOutputs"] = [
        {"id": c["uri"], "page_content": "", "metadata": {"doc_uri": c["uri"], "content_sha256": c["sha256"]}}
        for c in shown
    ]
    return out


def _attrs(span: Any) -> dict[str, Any]:
    out = dict(span.attributes)
    if "mlflow.spanOutputs" in out:
        out["mlflow.spanOutputs"] = json.loads(out["mlflow.spanOutputs"])
    return out


def test_doc_uri_by_id_form() -> None:
    find = require("reader.obs.find")
    for card in VEC7["cards"]:
        assert find.doc_uri(card["id"]) == card["uri"], card["id"]
        hashed = "reader://h/" + hashlib.sha256(card["id"].encode()).hexdigest()
        assert card["uri"] in (hashed, "reader://" + card["id"])
        assert URI.fullmatch(card["uri"])


def test_stage_7_identifier_and_concept_are_hashes() -> None:
    find = require("reader.obs.find")
    route = next(e for e in VEC7["find"] if e["name"] == "find.route")
    assert find.identifiers_sha256(route["stage7"]["identifiers"]) == route["done"]["identifiers_sha256"]
    # Order, case and whitespace runs do not change the hash; a different set does.
    assert find.identifiers_sha256(["b", "A"]) == find.identifiers_sha256(["a ", "B"])
    assert find.identifiers_sha256(["a"]) != find.identifiers_sha256(["a", "b"])
    assert find.concept_uri(route["stage7"]["concept_id"]) == route["done"]["concept_uri"]


def test_vec_obs_7_find_subtree() -> None:
    find = require("reader.obs.find")
    with tracing() as (handle, memory):
        run_vec7(find)
        handle.force_flush()
        got = {s.name: s for s in memory.get_finished_spans()}
    assert set(got) == {e["name"] for e in VEC7["find"]}
    top = got["find"]
    assert top.parent is None  # no current span: find is a trace root
    for entry in VEC7["find"]:
        span = got[entry["name"]]
        assert _attrs(span) == {**entry["expected"], **expected_docs(entry["name"])}, entry["name"]
        if span is not top:
            assert span.parent is not None and span.parent.span_id == top.context.span_id
            assert span.context.trace_id == top.context.trace_id
    # 26 cards give 25 documents; the 26th is counted only.
    assert "reader.doc.25.uri" not in got["find.select"].attributes
    assert got["find.select"].attributes["reader.find.cards"] == 26
    dumped = json.dumps([dict(s.attributes) for s in got.values()], ensure_ascii=False)
    for text in VEC7["hidden_text"]:
        assert text not in dumped, text


def test_find_under_a_job_root_is_its_child() -> None:
    find = require("reader.obs.find")
    with tracing() as (handle, memory):
        tracer = require("reader.obs").tracer()
        with tracer.start_as_current_span("job tutor.answer", attributes={"reader.job.kind": "tutor.answer"}):
            run_vec7(find)
        handle.force_flush()
        got = {s.name: s for s in memory.get_finished_spans()}
    assert got["find"].parent is not None
    assert got["find"].parent.span_id == got["job tutor.answer"].context.span_id


def test_judge_source_only_when_ran_and_skip_only_when_not() -> None:
    find = require("reader.obs.find")
    case = VEC7["judge_skipped"]
    with tracing() as (handle, memory):
        with find.judge_wait(input_sha256="d3" * 32, input_tokens=10) as part:
            part.done(kept=0, dropped=0, judge=find.Judge(**case["judge"]))
        handle.force_flush()
        (span,) = memory.get_finished_spans()
    a = dict(span.attributes)
    assert {k: v for k, v in a.items() if k.startswith("reader.judge_")} == case["expected"]
    assert find.Judge(ran=True, source="live", skip="gate").attributes() == {
        "reader.judge_ran": True, "reader.judge_source": "live"}
    assert find.Judge(ran=False, source="live", skip="error").attributes() == {
        "reader.judge_ran": False, "reader.judge_skip": "error"}


def _pattern_sample(pattern: str) -> str:
    samples = {
        "^[0-9a-f]{64}$": "ab" * 32,
        "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$": UUID_A,
        "^reader://h/[0-9a-f]{64}$": "reader://h/" + "cd" * 32,
        FAMILY["keys"]["uri"]["pattern"]: "reader://spn:" + "ef" * 32 + ":p12345:1234567-1234567#1234",
    }
    return samples[pattern]


def _valid(rule: dict[str, Any], key: str) -> Any:
    enums = {"models": "claude-sonnet-5-5", "find_routes": VEC7["catalogs"]["find_routes"][0],
             "facet_values.role": VEC7["catalogs"]["facet_values"]["role"][0],
             "facet_values.form": VEC7["catalogs"]["facet_values"]["form"][0],
             "topic_paths": VEC7["catalogs"]["topic_paths"][0]}
    if rule["type"] == "bool":
        return True
    if rule["type"] == "int":
        return rule["range"][1]
    if rule["type"] == "float":
        return float(rule["range"][1])
    if "enum" in rule:
        return rule["enum"][-1]
    if "enum_from" in rule:
        return enums[rule["enum_from"]]
    return _pattern_sample(rule["pattern"])


FIND_AND_PACK = ("find", "find.route", "find.retrieve", "find.preselect", "find.judge_wait", "find.select",
                 "pack.build", "link.run")


@pytest.mark.parametrize("name", FIND_AND_PACK)
def test_every_span_filled_at_the_cap_exports_every_key(name: str) -> None:
    """FR-OBS-36: the SDK's default span limit (128 attributes) keeps every listed key, documents at the cap
    included (find.select: 25 x 4 + 5)."""
    keys = list(VEC1["keys_by_span"][name])
    values = {k: _valid(VEC1["attributes"][k], k) for k in keys}
    for i in range(CAP):
        for part in FAMILY["spans"].get(name, []):
            values[f"reader.doc.{i}.{part}"] = _valid(FAMILY["keys"][part], part)
    if name in ("find", "find.judge_wait"):  # the exclusive pair: one side at a time
        values.pop("reader.judge_skip")
    assert len(values) <= 128
    with tracing() as (handle, memory):
        span = require("reader.obs").tracer().start_span(name, attributes=values)
        span.end()
        handle.force_flush()
        (out,) = memory.get_finished_spans()
    exported = {k: v for k, v in out.attributes.items() if not k.startswith("mlflow.")}
    assert exported == values
    if name == "find.select":
        assert len(values) == CAP * 4 + 5 == 105


@pytest.mark.parametrize("entry", ["pack_build", "link_run"])
def test_pack_build_and_link_run(entry: str) -> None:
    spans = require("reader.obs.spans")
    case = VEC7[entry]
    opener = {"pack_build": spans.pack_build, "link_run": spans.link_run}[entry]
    with tracing() as (handle, memory):
        with opener(**case["open"]) as part:
            part.done(**case["done"])
        handle.force_flush()
        (span,) = memory.get_finished_spans()
    assert span.name == case["name"]
    assert dict(span.attributes) == case["expected"]


def test_build_usd_never_yields_llm_cost() -> None:
    """FR-OBS-37: reader.build.usd is for reading only; a caller's reader.cost_usd on pack.build is dropped."""
    with tracing() as (handle, memory):
        span = require("reader.obs").tracer().start_span(
            "pack.build", attributes={"reader.build.usd": 0.5, "reader.cost_usd": 0.5, "reader.cost_input_usd": 0.1})
        span.end()
        handle.force_flush()
        (out,) = memory.get_finished_spans()
    a = dict(out.attributes)
    assert a == {"reader.build.usd": 0.5, "mlflow.spanType": "CHAIN"}


def test_status_ok_and_error_without_description() -> None:
    from opentelemetry.trace import StatusCode

    find = require("reader.obs.find")
    with tracing() as (handle, memory):
        with find.select() as part:
            part.done(cards=0, tokens=0, judged_tokens=0, unjudged_tokens=0, budget_tokens=0, docs=[])
        with pytest.raises(ValueError), find.retrieve(layer="L2", method="outline"):
            raise ValueError("bookcanary-7f3a9c")
        handle.force_flush()
        got = {s.name: s for s in memory.get_finished_spans()}
    assert got["find.select"].status.status_code == StatusCode.OK
    assert got["find.retrieve"].status.status_code == StatusCode.ERROR
    assert got["find.retrieve"].status.description is None
    assert "mlflow.spanOutputs" not in got["find.select"].attributes  # no documents: no preview


def test_helpers_do_nothing_with_tracing_off() -> None:
    find, spans = require("reader.obs.find"), require("reader.obs.spans")
    with obs_setup(enabled=False, **cats()) as (handle, memory):
        run_vec7(find)
        with spans.pack_build(**VEC7["pack_build"]["open"]) as part:
            part.done(**VEC7["pack_build"]["done"])
        handle.force_flush()
        assert list(memory.get_finished_spans()) == []


def test_helpers_accept_no_str() -> None:
    """FR-OBS-36: the openers and their `done` take ids, enums, counts and hashes only; no parameter is a plain
    `str` (a query, card or filter string has no way in). Enum parameters are the allow-list's members."""
    find, spans, attrs = require("reader.obs.find"), require("reader.obs.spans"), require("reader.obs.attributes")
    callables: list[Any] = []
    for module, names in ((find, ("find", "route", "retrieve", "preselect", "judge_wait", "select")),
                          (spans, ("pack_build", "link_run"))):
        for name in names:
            opener = getattr(module, name)
            callables.append(opener)
            part = typing.get_type_hints(opener)["return"]
            (part_type,) = typing.get_args(part)  # Iterator[<Part>] / ContextManager[<Part>]
            callables.append(part_type.done)
    callables += [find.Judge, find.Doc]

    def plain(annotation: Any) -> bool:
        return annotation is str or any(plain(a) for a in typing.get_args(annotation))

    for fn in callables:
        hints = typing.get_type_hints(fn.__init__ if isinstance(fn, type) else fn)
        hints.pop("return", None)
        for param, annotation in hints.items():
            assert not plain(annotation), (fn, param, annotation)
    literals = {"Channel": "reader.find.channel", "QuestionType": "reader.find.question_type",
                "JudgeSource": "reader.judge_source", "JudgeSkip": "reader.judge_skip", "Layer": "reader.layer",
                "Method": "reader.find.method", "Gate": "reader.find.gate",
                "Reliability": "reader.find.filter.reliability_min"}
    for alias, key in literals.items():
        assert list(typing.get_args(getattr(find, alias))) == list(attrs.ATTRIBUTES[key]["enum"]), alias
    assert list(typing.get_args(spans.OverlayKind)) == list(attrs.ATTRIBUTES["reader.overlay.kind"]["enum"])
    assert list(typing.get_args(spans.PackState)) == list(attrs.ATTRIBUTES["reader.pack.state"]["enum"])


_HOSTILE_ID = st.one_of(
    st.text(max_size=200),
    st.sampled_from([c["id"] for c in VEC7["cards"]]),
    st.builds(lambda p, s: f"spn:{'1f' * 32}:p{p}:{s}", st.integers(-5, 10**6), st.text(max_size=12)),
    st.builds(lambda tail: f"les:{UUID_A}{tail}", st.text(max_size=5)),
    st.builds(lambda tail: f"sec:{tail}", st.text(max_size=40)),
)


@settings(max_examples=300)
@given(card_id=_HOSTILE_ID)
def test_doc_uri_property(card_id: str) -> None:
    """INV-OBS-1's hostile card ids: every uri passes VEC-OBS-1's pattern; it is the id in clear only when the id is
    a PDF span locator, a finding or a lesson; otherwise it holds nothing of the id but its hash."""
    find = require("reader.obs.find")
    uri = find.doc_uri(card_id)
    assert URI.fullmatch(uri)
    if CLEAR.fullmatch(card_id):
        assert uri == "reader://" + card_id
    else:
        assert uri == "reader://h/" + hashlib.sha256(card_id.encode("utf-8", "surrogatepass")).hexdigest()
