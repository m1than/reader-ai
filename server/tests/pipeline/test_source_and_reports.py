# @trace VEC-PIPELINE-4, VEC-PIPELINE-5, VEC-PIPELINE-6, FR-PIPELINE-2, FR-PIPELINE-3, FR-PIPELINE-4, FR-PIPELINE-7, FR-PIPELINE-25, FR-PIPELINE-27, INV-PIPELINE-3
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
                    {
                        "cell": "e1",
                        "claim": "c1",
                        "verdict": "supported",
                        "spans": ["s1"],
                        "credibility": 2,
                        "note": "",
                    }
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
                {"cell": "ch1", "verdict": "supported", "spans": ["s1"], "credibility": 2, "note": ""},
                {"cell": "er1", "verdict": "supported", "spans": ["s2"], "credibility": 2, "note": ""},
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


# --- ADR-0039 revision: credibility, outcomes, lab references, the revise input ---------------------------------

CREDIBILITY = load_yaml(FIXTURES / "credibility.yaml")
B = "cd" * 32


@pytest.mark.parametrize("case", CREDIBILITY["cases"], ids=lambda c: c["name"])
def test_credibility_vectors(case: dict[str, Any]) -> None:
    """FR-PIPELINE-27 over VEC-PIPELINE-6."""
    reports = need("reader.pipeline.reports")
    spans = [s.replace("A:", f"{A}:").replace("B:", f"{B}:") for s in case["spans"]]
    assert reports.constrain_credibility(case["verdict"], case["given"], spans) == case["expect"]


def _credible_notebook() -> dict[str, Any]:
    built = _build(
        [
            {
                "id": "e1",
                "kind": "explain",
                "source": "x",
                "claims": [
                    {"id": "c1", "text": "t", "citations": [{"span": "s1"}]},
                    {"id": "c2", "text": "t", "citations": [{"span": "s2"}]},
                ],
            },
            {"id": "ch1", "kind": "changed", "source": "x", "as_of": "2026-11-01", "citations": [{"span": "s3"}]},
        ]
    )
    return dict(built.notebook)


def test_stamp_credibility_changes_only_credibility() -> None:
    """FR-PIPELINE-27: each judged claim and changed cell gets its value; nothing else changes."""
    reports = need("reader.pipeline.reports")
    findings = need("reader.pipeline.findings")
    nb = _credible_notebook()
    verdicts = [
        reports.Verdict("e1", "c1", "supported", (f"{A}:0",), "", None, 2),
        reports.Verdict("e1", "c2", "unsupported", (), "", None, 6),
        reports.Verdict("ch1", None, "contradicted", (f"{A}:2",), "", None, 5),
    ]
    stamped = findings.stamp_credibility(nb, verdicts)
    claims = stamped["cells"][0]["metadata"]["reader"]["claims"]
    assert [c["credibility"] for c in claims] == [2, 6]
    assert stamped["cells"][1]["metadata"]["reader"]["credibility"] == 5
    again = json.loads(json.dumps(stamped))
    for c in again["cells"][0]["metadata"]["reader"]["claims"]:
        del c["credibility"]
    del again["cells"][1]["metadata"]["reader"]["credibility"]
    assert again == nb
    # FR-PIPELINE-10: an item no fact_check judged gets 6 in the findings step
    filled = findings.fill_credibility(nb)
    assert [c["credibility"] for c in filled["cells"][0]["metadata"]["reader"]["claims"]] == [6, 6]


def test_model_written_credibility_is_dropped_and_lab_reference_kept() -> None:
    """FR-PIPELINE-3 (c) and FR-PIPELINE-25: credibility is code's; the drafter's reference is kept, its alias
    expanded to the span id, an unknown alias dropped; to_compact maps it back (round trip)."""
    notebook = need("reader.pipeline.notebook")
    lab = {
        "id": "lab1",
        "kind": "lab",
        "source": "x = 6 * 7",
        "kernel": "python3",
        "network": "none",
        "asserts": ["x == 42"],
        "reference": {"kind": "test_vector", "span": "s2", "expected": ["42"]},
    }
    explain = {
        "id": "e1",
        "kind": "explain",
        "source": "x",
        "credibility": 1,
        "claims": [{"id": "c1", "text": "t", "credibility": 1, "citations": [{"span": "s1"}]}],
    }
    built = _build([explain, lab])
    reader = built.notebook["cells"][0]["metadata"]["reader"]
    assert "credibility" not in reader and "credibility" not in reader["claims"][0]
    lab_reader = built.notebook["cells"][1]["metadata"]["reader"]
    assert lab_reader["reference"] == {"kind": "test_vector", "span": f"{A}:1", "expected": ["42"]}
    compact = notebook.to_compact(built.notebook, _aliases())
    assert compact["cells"][1]["reference"]["span"] == "s2"
    rebuilt = notebook.build(
        compact,
        title="L",
        aliases=_aliases(),
        material_title="Course title",
        stage="draft",
        prompt_version="v1",
        model="m",
        previous=None,
    )
    assert rebuilt.notebook == built.notebook  # build(to_compact(n)) == n
    unknown = _build([{**lab, "reference": {"kind": "test_vector", "span": "s99", "expected": ["42"]}}])
    assert unknown.notebook["cells"][0]["metadata"]["reader"]["reference"] == {
        "kind": "test_vector",
        "expected": ["42"],
    }
    assert unknown.stripped == frozenset()  # not a citation: the rule check reports it instead


def test_check_outcomes() -> None:
    """FR-PIPELINE-4: each check fails iff its rule; source_too_large does not fail the fact check."""
    reports = need("reader.pipeline.reports")
    failed = reports.check_failed
    assert failed("rules", {"failures": [{"rule": "claim_uncited"}]}) and not failed("rules", {"failures": []})
    lab_ok = {"lab_report": {"results": {"l": {"passed": True}}, "problems": []}}
    lab_bad = {"lab_report": {"results": {"l": {"passed": False}}, "problems": [{"kind": "lab_failed"}]}}
    assert failed("lab_verification", lab_bad) and not failed("lab_verification", lab_ok)
    for verdict, bad in (("supported", False), ("source_too_large", False), ("unsupported", True),
                         ("contradicted", True)):
        assert failed("fact_check", {"verdicts": [{"verdict": verdict}]}) is bad
    assert failed("adversarial_review", {"findings": [{"blocking": True}]})
    assert not failed("adversarial_review", {"findings": [{"blocking": False}]})


def test_revise_prompt_holds_only_the_failing_report() -> None:
    """FR-PIPELINE-3 (a): revise reads the notebook and the report of the check that failed, nothing else."""
    steps_mod = need("reader.pipeline.steps")
    runs = need("reader.pipeline.runs")
    source = need("reader.pipeline.source")
    from decimal import Decimal

    nb = _credible_notebook()
    lesson = runs.Lesson(source.render(synthetic_spans(3)), "L", "Course title")
    sid = lesson.source.aliases["s1"].span_id

    def row(n: int, stage: str, report: dict[str, Any]) -> Any:
        return runs.StepRow(n, n, stage, "", None, report, None, None, Decimal(0), Decimal(0))

    fact = {"verdicts": [{"cell": "e1", "claim": "c1", "verdict": "unsupported", "spans": [sid],
                          "note": "n", "forced": None, "credibility": 6}], "next": "revise"}
    steps = [row(1, "draft", {"stripped": []}), row(2, "rules", {"failures": [], "next": "lab_verification"}),
             row(3, "lab_verification", {"lab_report": {"results": {}, "problems": []}, "next": "fact_check"}),
             row(4, "fact_check", fact)]
    payload = json.loads(steps_mod._user_prompt("revise", nb, lesson, steps))
    assert payload["failed_check"] == "fact_check"
    assert payload["fact_report"][0]["spans"] == ["s1"] and "credibility" not in payload["fact_report"][0]
    assert set(payload) == {"notebook", "failed_check", "fact_report"}
    rules_failed = [row(1, "draft", {"stripped": []}),
                    row(2, "rules", {"failures": [{"rule": "claim_uncited", "cell": "e1", "claim": "c1",
                                                   "text": "uncited"}], "next": "revise"})]
    payload = json.loads(steps_mod._user_prompt("revise", nb, lesson, rules_failed))
    assert set(payload) == {"notebook", "failed_check", "rule_report"}


def test_review_prompt_asks_for_independent_lab_references() -> None:
    """FR-PIPELINE-25: the adversarial review's prompt (v1, edited in place) carries the instruction."""
    prompts = need("reader.pipeline.prompts")
    text = prompts.load().text("adversarial_review")
    assert "Lab references:" in text and "not independent of the lab's own computation" in text


# --- r3_gates_revision (FR-PIPELINE-7, -27, INV-PIPELINE-3): locator span ids carry their quote hash ------------

Q1, Q2, Q3 = "1" * 64, "2" * 64, "3" * 64  # synthetic quote hashes; synthetic texts, no book text


def _r3_aliases() -> dict[str, Any]:
    """Aliases over an r3 source: locators of material A with their stored hashes."""
    source = need("reader.pipeline.source")
    return {
        "s1": source.Span(f"spn:{A}:p61:0-10", 61, "text", "t1", Q1),
        "s2": source.Span(f"spn:{A}:p61:11-20", 61, "text", "t2", Q2),
        "s3": source.Span(f"spn:{A}:p62:0-0#0", 62, "figure", "", Q3),
    }


def test_expanded_locators_carry_their_quote_hash() -> None:
    """FR-PIPELINE-7: a locator citation gains its span's quote_sha256; a legacy id gets none."""
    reports = need("reader.pipeline.reports")
    source = need("reader.pipeline.source")
    errors = need("reader.agent.errors")
    aliases = {"s1": source.Span(f"{A}:4", 61, "text", "t"), "s2": _r3_aliases()["s1"]}
    kept, dropped = reports.expand_citations([{"span": "s1"}, {"span": "s2"}], aliases, "Title")
    assert dropped == []
    assert kept == [
        {"span": f"{A}:4", "page": 61, "material_title": "Title"},
        {"span": f"spn:{A}:p61:0-10", "quote_sha256": Q1, "page": 61, "material_title": "Title"},
    ]
    unhashed = {"s1": source.Span(f"spn:{A}:p61:0-10", 61, "text", "t")}  # a locator without its hash
    with pytest.raises(errors.AgentError) as refused:
        reports.expand_citations([{"span": "s1"}], unhashed, "Title")
    assert refused.value.code == "output_invalid"


def test_material_of_both_id_forms() -> None:
    notebook = need("reader.pipeline.notebook")
    locator = need("reader.ingest.locator")
    assert notebook.material_of(f"spn:{A}:p70:0-120") == A
    assert notebook.material_of(f"spn:{A}:p3:40-40#0") == A
    assert notebook.material_of(f"{A}:12") == A
    for bad in ("spn:abc", "a:b:3", "", f"spn:{A}:p1:9-3"):
        with pytest.raises(locator.LocatorError):
            notebook.material_of(bad)


def test_built_r3_notebook_carries_hashes_and_round_trips() -> None:
    """FR-PIPELINE-7: an explain claim, an erratum source_ref, a route's source_refs and a lab reference over
    r3 spans validate under SRV-LESSON's revised schema; to_compact gives back aliases without hashes."""
    notebook = need("reader.pipeline.notebook")
    from reader.lessons.schema import validate

    aliases = _r3_aliases()
    compact = {
        "cells": [
            {"id": "r1", "kind": "route", "source": "r", "source_refs": [{"span": "s3"}]},
            {"id": "e1", "kind": "explain", "source": "x",
             "claims": [{"id": "c1", "text": "t", "citations": [{"span": "s1"}]}]},
            {"id": "er1", "kind": "erratum", "source": "x", "correction": "c", "source_ref": {"span": "s2"},
             "citations": [{"span": "s2"}]},
            {"id": "lab1", "kind": "lab", "source": "x = 1", "kernel": "python3", "network": "none",
             "asserts": ["x == 1"], "reference": {"kind": "test_vector", "span": "s1", "expected": ["1"]}},
        ]
    }
    built = notebook.build(compact, title="L", aliases=aliases, material_title="M", stage="draft",
                           prompt_version="v1", model="m", previous=None)
    validate(notebook.to_bytes(built.notebook))
    readers = [c["metadata"]["reader"] for c in built.notebook["cells"]]
    assert readers[0]["source_refs"] == [
        {"material_sha256": A, "page": 62, "span": f"spn:{A}:p62:0-0#0", "quote_sha256": Q3}
    ]
    assert readers[1]["claims"][0]["citations"][0]["quote_sha256"] == Q1
    assert readers[2]["source_ref"] == {"material_sha256": A, "page": 61, "span": f"spn:{A}:p61:11-20",
                                        "quote_sha256": Q2}
    assert readers[3]["reference"] == {"kind": "test_vector", "span": f"spn:{A}:p61:0-10", "quote_sha256": Q1,
                                       "expected": ["1"]}
    back = json.dumps(notebook.to_compact(built.notebook, aliases))
    assert "quote_sha256" not in back and "spn:" not in back
    view = notebook.to_compact(built.notebook, aliases)["cells"]
    assert view[1]["claims"][0]["citations"] == [{"span": "s1"}]
    assert view[3]["reference"] == {"kind": "test_vector", "span": "s1", "expected": ["1"]}


def test_a_model_written_reference_hash_is_never_kept() -> None:
    """Code review round 1: a model-supplied quote_sha256 on a lab reference is overwritten for a known alias
    and dropped with the span for an unknown one (only code writes it, FR-PIPELINE-7)."""
    notebook = need("reader.pipeline.notebook")

    def reference(alias: str) -> dict[str, Any]:
        lab = {"id": "lab1", "kind": "lab", "source": "x = 1", "kernel": "python3", "network": "none",
               "asserts": ["x == 1"],
               "reference": {"kind": "test_vector", "span": alias, "quote_sha256": "f" * 64, "expected": ["1"]}}
        built = notebook.build({"cells": [lab]}, title="L", aliases=_r3_aliases(), material_title="M",
                               stage="draft", prompt_version="v1", model="m", previous=None)
        out: dict[str, Any] = built.notebook["cells"][0]["metadata"]["reader"]["reference"]
        return out

    assert reference("s1") == {"kind": "test_vector", "span": f"spn:{A}:p61:0-10", "quote_sha256": Q1,
                               "expected": ["1"]}
    assert reference("s99") == {"kind": "test_vector", "expected": ["1"]}


def test_legacy_id_notebook_under_r3_aliases_reads_as_unknown() -> None:
    """INV-PIPELINE-3 per step (round 1, blocking 3): a notebook drafted over the legacy pack, read by a later
    step whose source is the r3 pack: to_compact, the rule checks and the failing check's payload run; legacy
    ids read `unknown`; no span id or quote hash reaches the model."""
    notebook = need("reader.pipeline.notebook")
    rules = need("reader.pipeline.rules")
    stages = need("reader.pipeline.stages")
    steps_mod = need("reader.pipeline.steps")
    runs = need("reader.pipeline.runs")
    source = need("reader.pipeline.source")
    from decimal import Decimal

    legacy = notebook.build(
        {"cells": [{"id": "e1", "kind": "explain", "source": "x",
                    "claims": [{"id": "c1", "text": "t", "citations": [{"span": "s1"}]}]}]},
        title="L", aliases=_aliases(), material_title="M", stage="draft", prompt_version="v1", model="m",
        previous=None,
    ).notebook
    old_id = legacy["cells"][0]["metadata"]["reader"]["claims"][0]["citations"][0]["span"]
    r3 = _r3_aliases()
    view = notebook.to_compact(legacy, r3)
    assert view["cells"][0]["claims"][0]["citations"] == [{"span": "unknown"}]
    data = rules.RuleInput(legacy, {s.span_id: s for s in r3.values()}, set(), stages.load().reference_libraries)
    rules.report(stages.load().rules, data)  # runs without an exception
    lesson = runs.Lesson(source.render(list(r3.values())), "L", "Course title")

    def row(n: int, stage: str, report: dict[str, Any]) -> Any:
        return runs.StepRow(n, n, stage, "", None, report, None, None, Decimal(0), Decimal(0))

    fact = {"verdicts": [{"cell": "e1", "claim": "c1", "verdict": "unsupported", "spans": [old_id],
                          "note": "n", "forced": None, "credibility": 6}], "next": "revise"}
    steps = [row(1, "draft", {"stripped": []}), row(2, "fact_check", fact)]
    payload = json.loads(steps_mod._user_prompt("revise", legacy, lesson, steps))
    assert payload["fact_report"][0]["spans"] == ["unknown"]
    text = json.dumps(payload)
    assert old_id not in text and "quote_sha256" not in text and A not in text
