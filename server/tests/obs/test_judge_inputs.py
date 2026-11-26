"""SRV-OBS FR-OBS-38 [unit]: the judge_inputs copy's pure parts — the parsed output as 1-based ordinals into the
request's card ids, the validation codes that skip a copy, the consent decision, and the trace ids read from the
current span (never passed in).

@trace FR-OBS-38
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from .conftest import obs_setup, require

IDS = ["spn:a", "spn:b", "spn:c"]


def test_output_becomes_ordinals_into_the_card_ids() -> None:
    ji = require("reader.obs.judge_inputs")
    got = ji.ordinals(IDS, {"keep": ["spn:c", "spn:a"], "drop": ["spn:b"], "sufficiency": 0.75})
    assert got == {"keep": [3, 1], "drop": [2], "sufficiency": 0.75}
    assert ji.ordinals(IDS, {"keep": ["spn:b"], "sufficiency": 0.0})["drop"] == [1, 3]  # drop = the complement


@pytest.mark.parametrize(
    ("output", "code"),
    [
        ({"keep": ["spn:x"], "sufficiency": 0.5}, "unknown_card"),
        ({"keep": ["spn:a", "spn:a"], "sufficiency": 0.5}, "duplicate"),
        ({"keep": ["spn:a"], "sufficiency": math.nan}, "sufficiency"),
        ({"keep": ["spn:a"], "sufficiency": math.inf}, "sufficiency"),
        ({"keep": ["spn:a"], "sufficiency": "0.5"}, "sufficiency"),
        ({"keep": ["spn:a"], "sufficiency": True}, "sufficiency"),
        ({"keep": ["spn:a"], "sufficiency": 1.5}, "sufficiency"),
        ({"keep": "spn:a", "sufficiency": 0.5}, "shape"),
        ({"keep": ["spn:a"], "sufficiency": 0.5, "rationale": "text"}, "shape"),
    ],
)
def test_an_invalid_output_skips_with_a_code(output: dict[str, Any], code: str) -> None:
    ji = require("reader.obs.judge_inputs")
    with pytest.raises(ji.Skip) as skipped:
        ji.ordinals(IDS, output)
    assert skipped.value.code == code


def test_more_than_50_cards_skips() -> None:
    ji = require("reader.obs.judge_inputs")
    ids = [f"spn:{n}" for n in range(51)]
    with pytest.raises(ji.Skip) as skipped:
        ji.ordinals(ids, {"keep": ["spn:0"], "sufficiency": 0.5})
    assert skipped.value.code == "too_many_cards"


@pytest.mark.parametrize(
    ("role", "consent", "stored"),
    [("owner", False, True), ("owner", True, True), ("learner", True, True), ("learner", False, False)],
)
def test_consent_decides_whether_the_query_is_stored(role: str, consent: bool, stored: bool) -> None:
    assert require("reader.obs.judge_inputs").consenting(role, consent) is stored


def test_trace_ids_come_from_the_current_root_and_its_link() -> None:
    from opentelemetry.trace import Link

    ji = require("reader.obs.judge_inputs")
    assert ji.current_trace_ids() == (None, None)  # no span: nothing
    with obs_setup(role="worker") as (_handle, _memory):
        from reader.obs import tracer as reader_tracer

        tracer = reader_tracer()
        with tracer.start_as_current_span("find.judge_wait") as waiter:
            linked = waiter.get_span_context()
        with tracer.start_as_current_span("job judge.find", links=[Link(linked)]) as root:
            judge_id, find_id = ji.current_trace_ids()
        assert judge_id == f"{root.get_span_context().trace_id:032x}"
        assert find_id == f"{linked.trace_id:032x}"
