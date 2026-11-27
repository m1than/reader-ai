# @trace FR-PIPELINE-24, FR-PIPELINE-25, VEC-PIPELINE-7
"""The rule check over VEC-PIPELINE-7: each rule fires on its case and on no other, a clean notebook passes, the
failures come in cell order then rule order, and no model is ever touched (0 calls). [unit]"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from .conftest import FIXTURES, MATERIAL, load_yaml, need

RULES = load_yaml(FIXTURES / "rules.yaml")


def _id(span: str) -> str:
    return span.replace("A:", f"{MATERIAL}:")


def _cells(case: dict[str, Any]) -> list[dict[str, Any]]:
    if case.get("labs_over_time"):
        return [
            {
                "id": f"lab{i}",
                "reader": {"kind": "lab", "asserts": ["x == 1"], "reference": {"kind": "second_implementation"}},
            }
            for i in range(case["labs_over_time"])
        ]
    raw = RULES["clean_cells"] if case["cells"] == "clean" else case["cells"]
    out = []
    for cell in raw:
        reader = copy.deepcopy(cell["reader"])
        for claim in reader.get("claims", []):
            claim["citations"] = [{"span": _id(c["span"])} for c in claim["citations"]]
        if "citations" in reader:
            reader["citations"] = [{"span": _id(c["span"])} for c in reader["citations"]]
        reference = reader.get("reference")
        if isinstance(reference, dict) and "span" in reference:
            reference["span"] = _id(reference["span"])
        out.append({"id": cell["id"], "reader": reader})
    return out


def _input(case: dict[str, Any]) -> Any:
    rules = need("reader.pipeline.rules")
    source = need("reader.pipeline.source")
    stages = need("reader.pipeline.stages")
    notebook = {"cells": [{"id": c["id"], "metadata": {"reader": c["reader"]}} for c in _cells(case)]}
    spans = {_id(k): source.Span(_id(k), 61, "text", v) for k, v in RULES["spans"].items()}
    stripped = {(c, k) for c, k in case.get("stripped", [])}
    return rules.RuleInput(notebook, spans, stripped, stages.load().reference_libraries)


@pytest.mark.parametrize("case", RULES["cases"], ids=lambda c: c["name"])
def test_rule_vectors(case: dict[str, Any]) -> None:
    rules = need("reader.pipeline.rules")
    stages = need("reader.pipeline.stages")
    names = stages.load().rules
    failures = rules.check(names, _input(case))
    assert [[f.rule, f.cell, f.claim] for f in failures] == case["expect"]
    assert all(f.text and "Block" not in f.text and "ba7816" not in f.text for f in failures)  # ids only


def test_each_rule_fires_only_on_its_case() -> None:
    """A rule's own case is the only case of the table whose failures name it (besides the ordering case)."""
    rules = need("reader.pipeline.rules")
    stages = need("reader.pipeline.stages")
    names = stages.load().rules
    for name in names:
        firing = {
            c["name"]
            for c in RULES["cases"]
            if any(f.rule == name for f in rules.check(names, _input(c)))
        }
        expected = {c["name"] for c in RULES["cases"] if any(e[0] == name for e in c["expect"])}
        assert firing == expected, name


def test_the_rule_check_touches_no_model() -> None:
    """FR-PIPELINE-24: 0 calls; the rules take no provider at all (a provider that raises is never reachable)."""
    rules = need("reader.pipeline.rules")
    stages = need("reader.pipeline.stages")
    import inspect

    for function in rules.RULES.values():
        assert list(inspect.signature(function).parameters) == ["data"]
    report = rules.report(stages.load().rules, _input({"name": "clean", "cells": "clean"}))
    assert report == {"failures": []}
