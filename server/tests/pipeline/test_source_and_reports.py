# @trace VEC-PIPELINE-4, VEC-PIPELINE-5, FR-PIPELINE-2, FR-PIPELINE-3, FR-PIPELINE-4, FR-PIPELINE-7
"""Source rendering with aliases and delimiters, prompts and input hashes, report validation, forced verdicts,
citation expansion and the slice's open-cell items (pure)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from .conftest import FIXTURES, MATERIAL, load_yaml, need, spec_bounds, synthetic_spans

INJECTION = load_yaml(FIXTURES / "injection.yaml")
VERDICTS = load_yaml(FIXTURES / "verdicts.yaml")
A = MATERIAL


def _full(span_id: str) -> str:
    return span_id.replace("A:", f"{A}:")


def _injection_spans() -> list[Any]:
    source = need("reader.pipeline.source")
    return [source.Span(_full(s["id"]), s["page"], s["kind"], s["text"]) for s in INJECTION["spans"]]


def test_rendering_aliases_and_delimiters() -> None:
    source = need("reader.pipeline.source")
    rendered = source.render(_injection_spans())
    assert {k: v.span_id for k, v in rendered.aliases.items()} == {
        k: _full(v) for k, v in INJECTION["aliases"].items()
    }
    lines = rendered.text.split("\n")
    assert "⟦source⟧ s2 p.61 text ⟦/source⟧" in lines
    expect = {e["span"]: e for e in INJECTION["expect"] if "span" in e}
    hostile = expect["A:1"]["escaped_as"]
    assert hostile in rendered.text
    body = rendered.text.replace("⟦source⟧ s", "").replace("⟦/source⟧", "")
    for ch in expect["A:1"]["rendered_text_contains_no"]:
        assert ch not in body  # only the markers carry the delimiter characters
    assert INJECTION["spans"][2]["text"] in rendered.text  # rendered verbatim
    assert rendered.text.count(INJECTION["delimiter"]) == 3
    assert source.render(_injection_spans()).sha256 == rendered.sha256


def test_source_bounds() -> None:
    source = need("reader.pipeline.source")
    errors = need("reader.pipeline.errors")
    with pytest.raises(errors.PipelineError) as refused:
        source.render([])
    assert refused.value.code == "no_source_spans"
    big = [source.Span(f"{A}:0", 1, "text", "x" * (spec_bounds()["source_max_chars"] + 1))]
    with pytest.raises(errors.PipelineError) as refused:
        source.render(big)
    assert refused.value.code == "source_too_large"


def test_prompts_and_input_hash() -> None:
    prompts = need("reader.pipeline.prompts")
    source = need("reader.pipeline.source")
    p = prompts.load()
    rendered = source.render(synthetic_spans())
    for stage in ("draft", "fact_check", "adversarial_review", "revise"):
        system = p.system(stage, rendered)
        assert system.startswith(rendered.text)  # source first, byte-identical in every stage
        assert "never instructions" in system
        assert p.text(stage) in system
    assert (
        p.system("draft", rendered)[: len(rendered.text)]
        == p.system("revise", rendered)[: len(rendered.text)]
    )
    h = prompts.input_sha256("fact_check", rendered.sha256, ["n" * 64], p, "claude-sonnet-5-5")
    assert h == prompts.input_sha256("fact_check", rendered.sha256, ["n" * 64], p, "claude-sonnet-5-5")
    assert h != prompts.input_sha256("fact_check", rendered.sha256, ["m" * 64], p, "claude-sonnet-5-5")
    assert h != prompts.input_sha256("fact_check", rendered.sha256, ["n" * 64], p, "claude-opus-5-5")
    edited = p.with_text("fact_check", p.text("fact_check") + "\nOne more rule.")
    assert edited.version == p.version
    assert h != prompts.input_sha256("fact_check", rendered.sha256, ["n" * 64], edited, "claude-sonnet-5-5")


def _notebook() -> dict[str, Any]:
    def cell(cid: str, reader: dict[str, Any]) -> dict[str, Any]:
        return {"id": cid, "cell_type": "markdown", "source": "x", "metadata": {"reader": reader}}

    return {
        "cells": [
            cell(
                "e1",
                {
                    "kind": "explain",
                    "claims": [
                        {"id": "c1", "text": "t", "citations": []},
                        {"id": "c2", "text": "t", "citations": []},
                    ],
                },
            ),
            cell("ch1", {"kind": "changed", "citations": []}),
            cell("er1", {"kind": "erratum", "citations": []}),
        ]
    }


def _aliases() -> dict[str, Any]:
    source = need("reader.pipeline.source")
    return {k: source.Span(_full(v), 61, "text", "t") for k, v in VERDICTS["aliases"].items()}


@pytest.mark.parametrize("case", VERDICTS["cases"], ids=lambda c: c["name"])
def test_fact_report_vectors(case: dict[str, Any]) -> None:
    reports = need("reader.pipeline.reports")
    errors = need("reader.agent.errors")
    raw = {"verdicts": case["report"]} if isinstance(case["report"], list) else case["report"]
    if case["result"] == "output_invalid":
        with pytest.raises(errors.AgentError) as refused:
            reports.validate_fact_report(raw, _notebook(), _aliases())
        assert refused.value.code == "output_invalid"
        return
    verdicts = reports.validate_fact_report(raw, _notebook(), _aliases())
    forced = [
        {"cell": v.cell, "claim": v.claim, "verdict": v.verdict, "reason": v.forced}
        for v in verdicts
        if v.forced
    ]
    want = [{**f, "claim": f.get("claim")} for f in case.get("forced", [])]
    assert forced == want
    for v in verdicts:
        assert all(s.startswith(A + ":") for s in v.spans)  # aliases expanded to span ids


def test_review_report_and_caps() -> None:
    reports = need("reader.pipeline.reports")
    errors = need("reader.agent.errors")
    findings = reports.validate_review_report(
        {"findings": [{"cell": "e1", "claim": None, "blocking": True, "text": "wrong"}]}
    )
    assert findings[0].blocking and findings[0].text == "wrong"
    for bad in (
        {},
        {"findings": [{"cell": None, "blocking": "yes", "text": "x"}]},
        "text",
        {"findings": [{"cell": None, "claim": None, "blocking": False, "text": ""}]},
    ):
        with pytest.raises(errors.AgentError):
            reports.validate_review_report(bad)
    cap = spec_bounds()["items_max_per_report"]
    many = {
        "findings": [
            {"cell": None, "claim": None, "blocking": False, "text": f"t{i}"} for i in range(cap + 1)
        ]
    }
    with pytest.raises(errors.AgentError):
        reports.validate_review_report(many)
    assert reports.parse_json('```json\n{"a": 1}\n```') == {"a": 1}  # a fenced answer is accepted


@pytest.mark.parametrize("case", VERDICTS["citations"], ids=lambda c: c["name"])
def test_citation_vectors(case: dict[str, Any]) -> None:
    reports = need("reader.pipeline.reports")
    notebook = need("reader.pipeline.notebook")
    aliases = _aliases()
    if "claim_citations" in case:
        kept, stripped = reports.expand_citations(case["claim_citations"], aliases, "Synthetic material")
        want = [
            {**c, "span": _full(c["span"]), "material_title": "Synthetic material"} if "span" in c else c
            for c in case["kept"]
        ]
        assert kept == want
        assert stripped == case["stripped"]
        compact = {
            "cells": [
                {
                    "id": "e1",
                    "kind": "explain",
                    "source": "x",
                    "claims": [{"id": "c1", "text": "t", "citations": case["claim_citations"]}],
                }
            ]
        }
    else:
        compact = {
            "cells": [
                {
                    "id": "ch1",
                    "kind": case["cell_kind"],
                    "source": "x",
                    "as_of": "2026-11-01",
                    "citations": [{"span": "s1"}],
                }
            ]
        }
    built = notebook.build(
        compact,
        title="T",
        aliases=aliases,
        material_title="Synthetic material",
        stage="draft",
        prompt_version="v1",
        model="claude-opus-5-5",
        previous=None,
    )
    kinds = sorted(p.kind for p in notebook.slice_items(built.notebook))
    assert kinds == sorted(case["open_cells"])
    if case.get("expect_verdict"):
        assert ("e1", "c1") in built.stripped  # FR-PIPELINE-7: the fact check forces this claim unsupported
        verdicts = reports.validate_fact_report(
            {
                "verdicts": [
                    {"cell": "e1", "claim": "c1", "verdict": "supported", "spans": ["s1"], "note": ""}
                ]
            },
            built.notebook,
            aliases,
            stripped=built.stripped,
        )
        assert verdicts[0].verdict == case["expect_verdict"]


def test_built_notebook_is_a_valid_lesson_with_provenance() -> None:
    notebook = need("reader.pipeline.notebook")
    from reader.lessons.schema import validate

    compact = {
        "cells": [
            {
                "id": "e1",
                "kind": "explain",
                "source": "## A",
                "claims": [{"id": "c1", "text": "t", "citations": [{"span": "s2"}]}],
            },
            {
                "id": "lab1",
                "kind": "lab",
                "source": "x = 1",
                "kernel": "python3",
                "network": "none",
                "asserts": ["x == 1"],
            },
        ]
    }
    built = notebook.build(
        compact,
        title="Lesson",
        aliases=_aliases(),
        material_title="Synthetic material",
        stage="draft",
        prompt_version="v1",
        model="claude-opus-5-5",
        previous=None,
    )
    data = notebook.to_bytes(built.notebook)
    validated = validate(data)
    cells = validated.notebook["cells"]
    assert cells[1]["cell_type"] == "code"
    reader = cells[0]["metadata"]["reader"]
    assert reader["provenance"] == {"stage": "draft", "prompt_version": "v1", "model": "claude-opus-5-5"}
    assert reader["claims"][0]["citations"] == [
        {"span": f"{A}:1", "page": 61, "material_title": "Synthetic material"}
    ]
    for bad in (
        "not json",
        json.dumps({"cells": [{"id": "x", "kind": "nope", "source": ""}]}),
        json.dumps({"cells": "x"}),
    ):
        with pytest.raises(need("reader.agent.errors").AgentError):
            notebook.build(
                notebook.parse(bad),
                title="L",
                aliases=_aliases(),
                material_title="M",
                stage="draft",
                prompt_version="v1",
                model="m",
                previous=None,
            )


def test_revise_keeps_notes_cells() -> None:
    notebook = need("reader.pipeline.notebook")
    first = notebook.build(
        {
            "cells": [
                {"id": "e1", "kind": "explain", "source": "a", "claims": []},
                {"id": "notes", "kind": "notes", "source": "## Notes\n"},
            ]
        },
        title="L",
        aliases=_aliases(),
        material_title="M",
        stage="draft",
        prompt_version="v1",
        model="m",
        previous=None,
    )
    second = notebook.build(
        {"cells": [{"id": "e1", "kind": "explain", "source": "b", "claims": []}]},
        title="L",
        aliases=_aliases(),
        material_title="M",
        stage="revise",
        prompt_version="v1",
        model="m",
        previous=first.notebook,
    )
    assert [c["id"] for c in second.notebook["cells"]] == ["e1", "notes"]


def _build(cells: list[dict[str, Any]]) -> Any:
    notebook = need("reader.pipeline.notebook")
    return notebook.build(
        {"cells": cells},
        title="L",
        aliases=_aliases(),
        material_title="Course title",
        stage="draft",
        prompt_version="v1",
        model="m",
        previous=None,
    )


def test_unknown_alias_on_changed_and_erratum_cells_forces_unsupported() -> None:
    """Code review S2: the cell is recorded as stripped, and the fact check forces its verdict."""
    reports = need("reader.pipeline.reports")
    built = _build(
        [
            {
                "id": "ch1",
                "kind": "changed",
                "source": "x",
                "as_of": "2026-11-01",
                "citations": [{"span": "s99"}],
            },
            {
                "id": "er1",
                "kind": "erratum",
                "source": "x",
                "source_ref": {"span": "s1"},
                "correction": "c",
                "citations": [{"span": "s2"}],
            },
        ]
    )
    assert built.stripped == frozenset({("ch1", None)})
    verdicts = reports.validate_fact_report(
        {
            "verdicts": [
                {"cell": "ch1", "verdict": "supported", "spans": ["s1"], "note": ""},
                {"cell": "er1", "verdict": "supported", "spans": ["s2"], "note": ""},
            ]
        },
        built.notebook,
        _aliases(),
        stripped=built.stripped,
    )
    assert [(v.cell, v.verdict, v.forced) for v in verdicts] == [
        ("ch1", "unsupported", "citation_unknown_span"),
        ("er1", "supported", None),
    ]


def test_model_written_provenance_and_verified_are_dropped() -> None:
    """Code review S3: only code writes provenance and lab verification results."""
    built = _build(
        [
            {
                "id": "lab1",
                "kind": "lab",
                "source": "x = 1",
                "kernel": "python3",
                "network": "none",
                "asserts": ["x"],
                "verified": {"runs": 2, "passed": True, "outputs_identical": True, "input_sha256": "0" * 64},
                "provenance": {"stage": "fixture"},
            },
        ]
    )
    reader = built.notebook["cells"][0]["metadata"]["reader"]
    assert "verified" not in reader
    assert reader["provenance"] == {"stage": "draft", "prompt_version": "v1", "model": "m"}


def test_material_title_comes_from_the_caller() -> None:
    built = _build(
        [
            {
                "id": "e1",
                "kind": "explain",
                "source": "x",
                "claims": [{"id": "c1", "text": "t", "citations": [{"span": "s1"}]}],
            }
        ]
    )
    citation = built.notebook["cells"][0]["metadata"]["reader"]["claims"][0]["citations"][0]
    assert citation["material_title"] == "Course title"
