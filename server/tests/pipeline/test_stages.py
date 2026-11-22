# @trace VEC-PIPELINE-1, VEC-PIPELINE-2, FR-PIPELINE-5, INV-PIPELINE-1
"""The steps as data and the step decision (pure): VEC-PIPELINE-1's file, VEC-PIPELINE-2's sequences, and a
property over generated outcome sequences (INV-PIPELINE-1's loop bound and termination)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from .conftest import FIXTURES, load_yaml, need, spec_bounds

CASES = load_yaml(FIXTURES / "loop-cases.yaml")


def test_runtime_stages_file_equals_the_fixture() -> None:
    stages = need("reader.pipeline.stages")
    assert stages.STAGES_FILE.read_bytes() == (FIXTURES / "stages.yaml").read_bytes()
    p = stages.load()
    assert p.max_revise_loops == spec_bounds()["max_revise_loops"] == 1
    assert p.prompt_version == "v1"
    assert p.stages["draft"].task == "draft" and p.stages["revise"].task == "draft"
    assert p.stages["findings"].task is None
    assert p.stages["draft"].floor_reserve_usd == Decimal("0.61")
    assert p.stages["fact_check"].max_output_tokens == 16000


def _done(stages_mod: Any, results: list[dict[str, Any]]) -> list[Any]:
    done = []
    for r in results:
        problems = (r.get("unsupported", 0) + r.get("contradicted", 0) > 0) or r.get("blocking", 0) > 0
        done.append(stages_mod.Done(r["stage"], problems))
    return done


@pytest.mark.parametrize(
    "case",
    [c for c in CASES["cases"] if not any("invalid" in r or "error" in r for r in c["results"])],
    ids=lambda c: c["name"],
)
def test_sequence_of_each_case(case: dict[str, Any]) -> None:
    stages = need("reader.pipeline.stages")
    p = stages.load()
    done = _done(stages, case["results"])
    refused = bool(case.get("expect_refusals"))
    seq = [d.stage for d in done]
    while (nxt := stages.next_stage(p, done, refused=refused)) is not None:
        seq.append(nxt)
        done.append(stages.Done(nxt, False))
    assert seq == case["expect_stages"]


@given(st.lists(st.booleans(), min_size=6, max_size=6), st.booleans())
def test_loop_bound_and_termination(problems: list[bool], refuse_at_revise: bool) -> None:
    stages = need("reader.pipeline.stages")
    p = stages.load()
    done: list[Any] = []
    refused = False
    for _ in range(20):
        nxt = stages.next_stage(p, done, refused=refused)
        if nxt is None:
            break
        if nxt == "revise" and refuse_at_revise:
            refused = True
            continue
        done.append(stages.Done(nxt, problems[len(done) % 6]))
    else:
        pytest.fail("no termination")
    names = [d.stage for d in done]
    assert names.count("revise") <= p.max_revise_loops
    assert names[-2:] == ["findings", "lab_verification"]
    assert names[:3] == ["draft", "fact_check", "adversarial_review"]
