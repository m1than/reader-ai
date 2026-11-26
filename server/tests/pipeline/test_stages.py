# @trace VEC-PIPELINE-1, VEC-PIPELINE-2, FR-PIPELINE-5, FR-PIPELINE-23, FR-PIPELINE-26, INV-PIPELINE-1
"""The steps as data and the step decision (pure): VEC-PIPELINE-1's file, VEC-PIPELINE-2's sequences in the
four-check order, and a property over generated outcome sequences (INV-PIPELINE-1: the loop bound, every return
counted, no check before a cheaper one has passed, termination)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from .conftest import FIXTURES, load_yaml, need

CASES = load_yaml(FIXTURES / "loop-cases.yaml")
CHECKS = ["rules", "lab_verification", "fact_check", "adversarial_review"]


def test_runtime_stages_file_equals_the_fixture() -> None:
    stages = need("reader.pipeline.stages")
    assert stages.STAGES_FILE.read_bytes() == (FIXTURES / "stages.yaml").read_bytes()
    p = stages.load()
    assert p.max_loops == 3
    assert p.prompt_version == "v1"
    assert p.fact_source == "section"
    assert p.checks == tuple(CHECKS)
    assert p.first == ("draft",) and p.loop == ("revise",) and p.final == ("findings",)
    assert p.stages["draft"].task == "draft" and p.stages["revise"].task == "draft"
    assert p.stages["findings"].task is None and p.stages["rules"].task is None
    assert p.stages["draft"].floor_reserve_usd == Decimal("0.61")
    assert p.stages["fact_check"].max_output_tokens == 16000
    assert "secrets" not in p.reference_libraries and "hashlib" in p.reference_libraries


@pytest.mark.parametrize(
    ("change", "replacement"),
    [
        ("fact_check: { source: section }", "fact_check: { source: route }"),
        ("fact_check: { source: section }", "fact_check: { source: web }"),
        ("checks: [rules, lab_verification, fact_check, adversarial_review]", "checks: [rules, nowhere]"),
        ("rules: [explain_without_claims,", "rules: [no_such_rule, explain_without_claims,"),
    ],
)
def test_load_refuses_an_invalid_file(tmp_path: Path, change: str, replacement: str) -> None:
    """FR-PIPELINE-26: only `section` loads until the route lands; every step of `order` is declared; every rule
    has a function."""
    stages = need("reader.pipeline.stages")
    text = stages.STAGES_FILE.read_text(encoding="utf-8")
    assert change in text
    path = tmp_path / "stages.yaml"
    path.write_text(text.replace(change, replacement), encoding="utf-8")
    with pytest.raises(stages.StagesInvalid):
        stages.load(path)


def _failed(stage: str, notebook: dict[str, Any], results: list[dict[str, Any]]) -> bool:
    if stage == "rules":
        return notebook.get("rules") == "fail"
    if stage == "lab_verification":
        return notebook.get("lab") == "fail"
    r = results.pop(0)
    assert r["stage"] == stage, (stage, r)
    if stage == "fact_check":
        return bool(r.get("unsupported", 0) + r.get("contradicted", 0) > 0)
    return bool(r.get("blocking", 0) > 0)


def simulate(case: dict[str, Any]) -> tuple[list[str], str | None, int]:
    """The run's step sequence from the case's outcomes, through `decide` and `next_stage` only."""
    stages = need("reader.pipeline.stages")
    p = stages.load()
    results = list(case["results"])
    answers = list(case.get("loop_fits", []))
    done: list[Any] = []
    refused = False
    loops = 0
    notebook: dict[str, Any] = {}
    while (nxt := stages.next_stage(p, done, refused=refused)) is not None:
        if results and results[0]["stage"] == nxt and "refused" in results[0]:
            results.pop(0)
            refused = True  # FR-PIPELINE-5: a refused model step writes no step; findings follows
            continue
        if nxt in ("draft", "revise"):
            notebook = results.pop(0)
            done.append(stages.Done(nxt))
            continue
        if nxt in CHECKS:
            failed = _failed(nxt, notebook, results)
            decision, stop, _ = stages.decide(p, nxt, failed, loops, lambda: answers.pop(0))
            loops += decision == "revise"
            refused = refused or stop == "loop_cap"
            done.append(stages.Done(nxt, failed, decision, stop))
            continue
        done.append(stages.Done(nxt))
    assert answers == [] and results == [], "every listed outcome and loop answer was used"
    stop = next((d.stop for d in reversed(done) if d.stop), None)
    return [d.stage for d in done], stop, loops


@pytest.mark.parametrize(
    "case",
    [c for c in CASES["cases"] if not any("invalid" in r or "error" in r for r in c["results"])],
    ids=lambda c: c["name"],
)
def test_sequence_of_each_case(case: dict[str, Any]) -> None:
    seq, stop, loops = simulate(case)
    assert seq == case["expect_stages"]
    assert loops == case["expect_loops"]
    if case["expect_stop"] in ("passed", "loop_limit", "loop_cap"):
        assert stop == case["expect_stop"]


def test_a_draft_failing_each_check_once_stops_at_the_fourth_failure() -> None:
    """ADR-0039 `verified_by`: rules, lab, claim support and review fail once each: three loops, no fourth."""
    case = next(c for c in CASES["cases"] if c["name"] == "four-checks-fail-once")
    seq, stop, loops = simulate(case)
    assert loops == 3 and seq.count("revise") == 3 and stop == "loop_limit"
    assert seq[-2:] == ["adversarial_review", "findings"]


def test_refusal_wins_over_a_recorded_next() -> None:
    """FR-PIPELINE-5 (round 5): with a refusal, the next step is findings whatever `next` says."""
    stages = need("reader.pipeline.stages")
    p = stages.load()
    done = [stages.Done("draft"), stages.Done("rules", True, "revise")]
    assert stages.next_stage(p, done, refused=False) == "revise"
    assert stages.next_stage(p, done, refused=True) == "findings"
    assert stages.next_stage(p, [], refused=True) is None


@given(
    st.lists(st.booleans(), min_size=20, max_size=20),
    st.lists(st.sampled_from([None, None, "run", "task", "month"]), min_size=4, max_size=4),
)
def test_loop_bound_order_and_termination(failures: list[bool], answers: list[str | None]) -> None:
    """INV-PIPELINE-1: at most 3 loops, every return to revise counted, checks in order with no check before a
    cheaper one passed on the same notebook, a refused loop never starts, termination with findings last."""
    stages = need("reader.pipeline.stages")
    p = stages.load()
    done: list[Any] = []
    refused, loops, k = False, 0, 0
    asked = list(answers)
    for _ in range(80):
        nxt = stages.next_stage(p, done, refused=refused)
        if nxt is None:
            break
        if nxt in CHECKS:
            failed = failures[k % len(failures)]
            k += 1
            decision, stop, _ = stages.decide(p, nxt, failed, loops, lambda: asked.pop(0) if asked else None)
            loops += decision == "revise"
            refused = refused or stop == "loop_cap"
            done.append(stages.Done(nxt, failed, decision, stop))
        else:
            done.append(stages.Done(nxt))
    else:
        pytest.fail("no termination")
    names = [d.stage for d in done]
    assert names.count("revise") == loops <= p.max_loops
    assert names[0] == "draft" and names[-1] == "findings"
    passes: list[list[Any]] = []
    for d in done:
        if d.stage in ("draft", "revise"):
            passes.append([])
        elif d.stage in CHECKS:
            passes[-1].append(d)
    for checks in passes:
        assert [d.stage for d in checks] == CHECKS[: len(checks)]
        assert all(not d.problems for d in checks[:-1])
