# @trace VEC-PIPELINE-3, FR-PIPELINE-8, FR-PIPELINE-10, INV-PIPELINE-5
"""The G2 report on F01-based cases, the findings step's open cells and the deterministic deltas (pure)."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from .conftest import F01, FIXTURES, LESSON_FIXTURES, apply_ops, load_yaml, need

G2 = load_yaml(FIXTURES / "g2-cases.yaml")
LESSON_CASES = {c["id"]: c for c in load_yaml(LESSON_FIXTURES / "invalid.yaml")["cases"]}


def _all_supported(nb: dict[str, Any]) -> dict[tuple[str, str | None], str]:
    out: dict[tuple[str, str | None], str] = {}
    for cell in nb["cells"]:
        reader = cell["metadata"].get("reader", {})
        if reader.get("kind") == "explain":
            for claim in reader.get("claims", []):
                out[(cell["id"], claim["id"])] = "supported"
        if reader.get("kind") in ("changed", "erratum"):
            out[(cell["id"], None)] = "supported"
    return out


@pytest.mark.parametrize("case", G2["cases"], ids=lambda c: c["id"])
def test_g2_vectors(case: dict[str, Any]) -> None:
    g2 = need("reader.pipeline.g2")
    findings = need("reader.pipeline.findings")
    verify = need("reader.pipeline.verify")
    base = json.loads(F01)
    ops = LESSON_CASES[case["from"].split("/")[1]]["ops"] if "from" in case else case.get("ops", [])
    final = apply_ops(base, ops)
    model_written = copy.deepcopy(final)
    if case.get("tamper") == "final_revision_extra_cell":
        extra = copy.deepcopy(final["cells"][-1])
        extra["id"] = "tampered"
        final["cells"].append(extra)
    results = {
        cell["id"]: cell["metadata"]["reader"]["verified"]
        for _, cell in findings.labs_of(final)
        if "verified" in cell["metadata"]["reader"]
    }
    lab_report = verify.LabReport(findings.labs_sha256(base), results, [])
    records = set(case.get("records", ["fact_check", "adversarial_review", "lab_verification"]))
    deltas_ok = g2.deltas_ok(final, model_written, [], lab_report)
    report = g2.report(final, records, lab_report, _all_supported(final), deltas_ok)
    assert report["publishable"] is case["publishable"]
    assert report["failing"] == case["failing"]


def test_open_cell_ids_are_stable_and_collapse() -> None:
    findings = need("reader.pipeline.findings")
    a = findings.Problem("adversarial_review", "e1", None, "same text", "adversarial_review", False)
    b = findings.Problem("adversarial_review", "e1", None, "same text", "adversarial_review", True)
    c = findings.Problem("fact_check", "e1", "c1", "unsupported: c1", "fact_check", True)
    cid = findings.open_cell_id(a)
    assert cid.startswith("f-") and len(cid) == 18 and cid == findings.open_cell_id(b)
    nb = json.loads(F01)
    once = findings.append_findings(nb, [a, b, c])
    added = once["cells"][len(nb["cells"]) :]
    assert len(added) == 2
    merged = next(x for x in added if x["id"] == cid)
    assert merged["metadata"]["reader"]["finding"] == {
        "stage": "adversarial_review",
        "blocking": True,
        "text": "same text",
    }
    assert merged["metadata"]["reader"]["provenance"] == {"stage": "findings"}
    twice = findings.append_findings(once, [a, b, c])  # a rerun neither duplicates nor renumbers
    assert twice == once
    notes_before = [x for x in nb["cells"] if x["metadata"]["reader"]["kind"] == "notes"]
    assert [x for x in twice["cells"] if x["metadata"]["reader"]["kind"] == "notes"] == notes_before


def test_problems_from_reports_and_slice_items() -> None:
    findings = need("reader.pipeline.findings")
    reports = need("reader.pipeline.reports")
    verdicts = [
        reports.Verdict("e1", "c1", "unsupported", (), "no support", None),
        reports.Verdict("e1", "c2", "supported", ("x:1",), "", None),
        reports.Verdict("er1", None, "contradicted", ("x:2",), "", None),
    ]
    review = [reports.Finding("e1", None, True, "unclear"), reports.Finding(None, None, False, "typo")]
    problems = findings.problems_from(verdicts, review, json.loads(F01))
    blocking = sorted((p.stage, p.kind) for p in problems if p.blocking)
    assert blocking == [
        ("adversarial_review", "adversarial_review"),
        ("fact_check", "fact_check"),
        ("fact_check", "fact_check"),
    ]
    nonblocking = sorted(p.kind for p in problems if not p.blocking)
    # F01 carries a changed cell and URL citations: the slice's unchecked items (non-blocking)
    assert nonblocking == ["adversarial_review", "changed_unchecked", "url_citation_unchecked"]


def test_final_revision_is_the_deterministic_deltas() -> None:
    """INV-PIPELINE-5: F = stamp_labs(append_findings(M, problems), lab_report); any other F fails."""
    findings = need("reader.pipeline.findings")
    verify = need("reader.pipeline.verify")
    g2 = need("reader.pipeline.g2")
    m = json.loads(F01)
    lab = next(cell for _, cell in findings.labs_of(m))
    del lab["metadata"]["reader"]["verified"]
    problems = [findings.Problem("adversarial_review", None, None, "x", "adversarial_review", False)]
    report = verify.LabReport(
        findings.labs_sha256(m),
        {
            "f01-lab": {
                "runs": 2,
                "passed": False,
                "outputs_identical": True,
                "input_sha256": findings.source_sha256(lab),
            }
        },
        [findings.Problem("lab_failed", "f01-lab", None, "assert failed", "lab_verification", True)],
    )
    final = findings.stamp_labs(findings.append_findings(m, problems), report)
    assert g2.deltas_ok(final, m, problems, report)
    stamped = next(cell for _, cell in findings.labs_of(final))
    assert stamped["metadata"]["reader"]["verified"]["passed"] is False
    opened = [c for c in final["cells"] if c["metadata"]["reader"]["kind"] == "open"]
    assert {c["metadata"]["reader"]["finding"]["stage"] for c in opened} >= {"lab_verification"}
    tampered = copy.deepcopy(final)
    tampered["cells"][0]["source"] = "changed by hand"
    assert not g2.deltas_ok(tampered, m, problems, report)
