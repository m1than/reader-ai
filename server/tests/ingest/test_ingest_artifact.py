"""Artifact format, normalisation and validation (SRV-INGEST FR-INGEST-6, -7, -10, -11, -17; INV-INGEST-1).

@trace FR-INGEST-6 FR-INGEST-7 FR-INGEST-10 FR-INGEST-11 FR-INGEST-17 INV-INGEST-1 VEC-INGEST-1
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from . import make_artifact_fixture as fixture_gen
from .conftest import ARTIFACT_OK_ID, artifact_files, build_artifact, canonical, need, write_dir


def validate(tmp: Path, files: dict[str, bytes]) -> Any:
    artifact = need("reader.ingest.artifact")
    root = write_dir(tmp, files)
    return artifact.validate_dir(root)


def code_of(tmp: Path, files: dict[str, bytes]) -> str:
    artifact = need("reader.ingest.artifact")
    with pytest.raises(artifact.ArtifactError) as failure:
        validate(tmp, files)
    return str(failure.value.code)


# ---- VEC-INGEST-1, FR-INGEST-10 ----


def test_fixture_artifact_is_valid_and_its_id_is_the_manifest_hash(tmp_path: Path) -> None:
    artifact = need("reader.ingest.artifact")
    files = artifact_files()
    manifest = validate(tmp_path, files)
    assert manifest["span_count"] == 8
    assert artifact.artifact_id(files["manifest.json"]) == ARTIFACT_OK_ID
    assert hashlib.sha256(files["manifest.json"]).hexdigest() == ARTIFACT_OK_ID


def test_converter_version_constant() -> None:
    ingest = need("reader.ingest")
    assert ingest.CONVERTER_VERSION == "docling-2.151.1+r3"
    assert str(ingest.BLOBS_DIR) == "/var/lib/reader/blobs"


def test_write_artifact_round_trips_and_is_canonical(tmp_path: Path) -> None:
    artifact = need("reader.ingest.artifact")
    spans = [json.loads(x) for x in artifact_files()["spans.jsonl"].splitlines()]
    outline = json.loads(artifact_files()["outline.json"])
    out = tmp_path / "out"
    ident = artifact.write_artifact(
        out,
        material_sha256="a" * 64,
        page_count=2,
        pages_converted=2,
        pages_without_text=[2],
        outline_source="bookmarks",
        spans=spans,
        outline=outline,
    )
    written = {p.name: p.read_bytes() for p in out.iterdir()}
    assert written == artifact_files()
    assert ident == ARTIFACT_OK_ID


def test_non_canonical_manifest_is_rejected(tmp_path: Path) -> None:
    files = artifact_files()
    files["manifest.json"] = json.dumps(json.loads(files["manifest.json"]), indent=1).encode()
    assert code_of(tmp_path, files) == "manifest_not_canonical"


def test_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    files = artifact_files()
    files["outline.json"] = canonical([])
    assert code_of(tmp_path, files) == "hash_mismatch"


def test_incomplete_conversion_is_rejected(tmp_path: Path) -> None:
    assert code_of(tmp_path, build_artifact(pages_converted=1)) == "incomplete"


def test_extra_manifest_key_is_a_schema_error(tmp_path: Path) -> None:
    assert code_of(tmp_path, build_artifact(extra="x")) == "schema"


# ---- FR-INGEST-6, -7: normalisation ----


def test_normalise_text() -> None:
    artifact = need("reader.ingest.artifact")
    raw = "Café inter­national\x00\x07 line\nnext\ttab \ud800"
    out = artifact.normalise(raw, code=False)
    assert out == "Café international line\nnext\ttab "


def test_normalise_code_keeps_soft_hyphen_and_line_breaks() -> None:
    artifact = need("reader.ingest.artifact")
    raw = "$ a  \r\nb­ c   \r\n\x01d"
    assert artifact.normalise(raw, code=True) == "$ a\nb­ c\nd"


@pytest.mark.parametrize(
    ("raw", "code", "want"),
    [
        (" \x1f", True, ""),  # the control goes first, then the trailing space
        ("a \r", True, "a"),  # a lone CR is a control: removed, then the space before it
        ("x \x02\ny", True, "x\ny"),  # pdfium's U+0002 at a line end
        ("e­́", False, "é"),  # U+00AD goes before NFC, so the accent composes
        ("e\x01́", False, "é"),  # a control goes before NFC too (review: the NFC-first mutant)
        ("e\x01́", True, "é"),
        ("a\t\nb", True, "a\t\nb"),  # only spaces are stripped, never a tab (review: the bare-rstrip mutant)
        ("a\rb", True, "ab"),  # a lone CR is a control, not a line break: a known join
    ],
)
def test_normalise_removes_before_it_strips_and_composes(raw: str, code: bool, want: str) -> None:
    artifact = need("reader.ingest.artifact")
    assert artifact.normalise(raw, code=code) == want


@settings(max_examples=300)
@example(" \x1f", True)
@example("e­́", False)
@example("e\x01́", False)
@example("e\x01́", True)
@given(st.text(max_size=40), st.booleans())
def test_normalise_is_idempotent(text: str, code: bool) -> None:
    """INV-INGEST-1: normalise(x) is accepted by the validator and is a fixpoint."""
    artifact = need("reader.ingest.artifact")
    once = artifact.normalise(text, code=code)
    assert artifact.normalise(once, code=code) == once
    assert artifact._normalised(once, code=code)


def test_legacy_span_id_is_retired() -> None:
    artifact = need("reader.ingest.artifact")
    assert not hasattr(artifact, "span_id")  # FR-INGEST-17 retired: the `<sha>:<seq>` form is never issued


# ---- INV-INGEST-1: each rule broken one at a time ----

SPAN = {
    "seq": 0, "page": 1, "kind": "text", "level": None, "decoded": None, "text": "x", "bbox": [0, 0, 1, 1]
}

BROKEN_SPANS: list[tuple[str, dict[str, Any]]] = [
    ("page 0", {"page": 0}),
    ("page over count", {"page": 3}),
    ("unknown kind", {"kind": "poem"}),
    ("level on text", {"level": 1}),
    ("heading level 17", {"kind": "heading", "level": 17}),
    ("heading without level", {"kind": "heading", "level": None}),
    ("decoded on text", {"decoded": True}),
    ("formula without decoded", {"kind": "formula", "decoded": None}),
    ("bbox over 1e5", {"bbox": [0, 0, 100001, 1]}),
    ("huge number", {"page": 10**400}),
    ("nul", {"text": "a\x00b"}),
    ("c0 control", {"text": "a\x1bb"}),
    ("lone surrogate", {"text": "a\ud800b"}),
    ("not nfc", {"text": "Café"}),
    ("soft hyphen in text", {"text": "inter­national"}),
    ("bbox inverted", {"bbox": [5, 0, 1, 1]}),
    ("bbox negative", {"bbox": [-1, 0, 1, 1]}),
    ("bbox nan", {"bbox": [math.nan, 0, 1, 1]}),
    ("bbox inf", {"bbox": [0, 0, math.inf, 1]}),
    ("bbox short", {"bbox": [0, 0, 1]}),
    ("extra key", {"extra": 1}),
    ("text too long", {"text": "x" * (1024 * 1024 + 1)}),
]


@pytest.mark.parametrize(("label", "change"), BROKEN_SPANS, ids=[c[0] for c in BROKEN_SPANS])
def test_each_span_rule(tmp_path: Path, label: str, change: dict[str, Any]) -> None:
    span = {**SPAN, **change}
    try:
        files = build_artifact(spans=[span])
    except ValueError:  # NaN and inf cannot be written as canonical JSON: write them raw
        body = json.dumps(span, sort_keys=True, separators=(",", ":"), allow_nan=True).encode() + b"\n"
        files = build_artifact(spans=[SPAN])
        files["spans.jsonl"] = body  # NaN/inf written raw
        meta = json.loads(files["manifest.json"])
        meta["spans_sha256"] = hashlib.sha256(body).hexdigest()
        files["manifest.json"] = canonical(meta)
    assert code_of(tmp_path, files) == "schema", label


def test_code_span_may_hold_soft_hyphen(tmp_path: Path) -> None:
    files = build_artifact(spans=[{**SPAN, "kind": "code", "text": "a­b"}])
    assert validate(tmp_path, files)["span_count"] == 1


def test_seq_gap_is_rejected(tmp_path: Path) -> None:
    files = build_artifact(spans=[SPAN, {**SPAN, "seq": 2}])
    assert code_of(tmp_path, files) == "schema"


OUTLINE = {"seq": 0, "level": 1, "title": "t", "page": 1}
BROKEN_OUTLINE: list[tuple[str, Any]] = [
    ("not a list", {"seq": 0}),
    ("level 0", [{**OUTLINE, "level": 0}]),
    ("level 17", [{**OUTLINE, "level": 17}]),
    ("page over count", [{**OUTLINE, "page": 9}]),
    ("title too long", [{**OUTLINE, "title": "x" * 1025}]),
    ("title control", [{**OUTLINE, "title": "a\x07"}]),
    ("missing key", [{"seq": 0, "level": 1, "title": "t"}]),
    ("extra key", [{**OUTLINE, "x": 1}]),
    ("seq gap", [{**OUTLINE, "seq": 1}]),
]


@pytest.mark.parametrize(("label", "outline"), BROKEN_OUTLINE, ids=[c[0] for c in BROKEN_OUTLINE])
def test_each_outline_rule(tmp_path: Path, label: str, outline: Any) -> None:
    assert code_of(tmp_path, build_artifact(outline=outline)) == "schema", label


def test_pages_without_text_must_be_sorted_unique_in_range(tmp_path: Path) -> None:
    for bad in ([2, 1], [1, 1], [3]):
        assert code_of(tmp_path / str(bad), build_artifact(pages_without_text=bad)) == "schema"


texts = st.text(alphabet=st.characters(codec="utf-8", exclude_categories=("Cs",)), max_size=40)


@settings(max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture])
@example(rows=[("code", " \x1f")])  # investigator, 2026-11-24: not idempotent before the order fix
@example(rows=[("code", "a \r")])
@example(rows=[("text", "e­́")])
@given(st.lists(st.tuples(st.sampled_from(["text", "code", "list_item"]), texts), min_size=1, max_size=6))
def test_valid_generated_artifacts_pass(tmp_path: Path, rows: list[tuple[str, str]]) -> None:
    artifact = need("reader.ingest.artifact")
    spans = [
        {**SPAN, "seq": i, "kind": kind, "text": artifact.normalise(text, code=kind == "code")}
        for i, (kind, text) in enumerate(rows)
    ]
    out = tmp_path / hashlib.sha256(repr(rows).encode()).hexdigest()[:12]
    artifact.write_artifact(
        out,
        material_sha256="a" * 64,
        page_count=2,
        pages_converted=2,
        pages_without_text=[],
        outline_source="headings",
        spans=spans,
        outline=[],
    )
    assert artifact.validate_dir(out)["span_count"] == len(spans)
    written = [json.loads(x) for x in (out / "spans.jsonl").read_bytes().splitlines()]
    assert written == fixture_gen.with_locators(spans, "a" * 64)  # INV-INGEST-1 rev: valid locators


# ---- Revision 3 (FR-INGEST-10, -11, INV-INGEST-1 revised): format 2, locators, pack id ----


def _fixture_spans() -> list[dict[str, Any]]:
    return [json.loads(x) for x in artifact_files()["spans.jsonl"].splitlines()]


def test_format_two_manifest_and_span_fields() -> None:
    meta = json.loads(artifact_files()["manifest.json"])
    assert meta["format"] == "reader-ingest/2"
    assert meta["parse_config"] == fixture_gen.PARSE_CONFIGS["docling-2.151.1+r3"]
    assert meta["pack_id"] == fixture_gen.pack_fields("a" * 64, "docling-2.151.1+r3")["pack_id"]
    assert all({"locator", "quote_sha256"} <= set(s) for s in _fixture_spans())


def _mutated(index: int, **change: Any) -> list[dict[str, Any]]:
    spans = _fixture_spans()
    spans[index] = {**spans[index], **change}
    return spans


LOCATOR_BREAKS: list[tuple[str, int, dict[str, Any]]] = [
    ("wrong offsets", 1, {"locator": "spn:" + "a" * 64 + ":p1:8-30"}),
    ("another page", 1, {"locator": "spn:" + "a" * 64 + ":p2:8-31"}),
    ("another material", 0, {"locator": "spn:" + "b" * 64 + ":p1:0-7"}),
    ("missing ordinal", 5, {"locator": "spn:" + "a" * 64 + ":p2:9-9#1"}),
    ("not a locator", 0, {"locator": "a" * 64 + ":0"}),
    ("wrong quote", 2, {"quote_sha256": "0" * 64}),
    ("quote of other text", 2, {"quote_sha256": hashlib.sha256(b"Glava").hexdigest()}),
]


@pytest.mark.parametrize(("label", "index", "change"), LOCATOR_BREAKS, ids=[c[0] for c in LOCATOR_BREAKS])
def test_each_locator_rule_broken_alone_is_locator_mismatch(
    tmp_path: Path, label: str, index: int, change: dict[str, Any]
) -> None:
    files = build_artifact(spans=_mutated(index, **change), keep_locators=True)
    assert code_of(tmp_path, files) == "locator_mismatch", label


def test_missing_locator_key_is_schema(tmp_path: Path) -> None:
    spans = _fixture_spans()
    del spans[0]["locator"]
    assert code_of(tmp_path, build_artifact(spans=spans, keep_locators=True)) == "schema"


def test_format_one_is_refused(tmp_path: Path) -> None:
    assert code_of(tmp_path, build_artifact(format="reader-ingest/1")) == "schema"


def test_wrong_pack_id_or_unpinned_parse_config_is_pack_mismatch(tmp_path: Path) -> None:
    assert code_of(tmp_path / "a", build_artifact(pack_id="0" * 64)) == "pack_mismatch"
    config = {**fixture_gen.PARSE_CONFIGS["docling-2.151.1+r3"], "batch_pages": 10}
    pid = hashlib.sha256(canonical({"material_sha256": "a" * 64, "parse_config": config})).hexdigest()
    assert code_of(tmp_path / "b", build_artifact(parse_config=config, pack_id=pid)) == "pack_mismatch"


def test_unknown_converter_version_is_version_mismatch(tmp_path: Path) -> None:
    assert code_of(tmp_path, build_artifact(converter_version="docling-9+r9")) == "version_mismatch"


def test_r2_artifact_in_format_two_validates_with_its_own_pack(tmp_path: Path) -> None:
    meta = validate(tmp_path, build_artifact(converter_version="docling-2.151.1+r2"))
    assert meta["pack_id"] == fixture_gen.pack_fields("a" * 64, "docling-2.151.1+r2")["pack_id"]
