"""SRV-OBS FR-OBS-39 [unit]: local judge evaluation — `start`'s refusals before any row, the eval Run's metrics
computed by code from the Postgres rows (nearest-rank percentiles, the judged-path completion denominator, per
question type, caller `none`), and the Run snapshot passing VEC-OBS-5 `runs.eval`.

@trace FR-OBS-39 VEC-OBS-5
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from .conftest import require

SET = "questions:\n  - {id: e01, question: What is a hardened key?, question_type: exact, chapter: 3}\n"
SLOTS = ("judge.eval.relevance", "judge.eval.sufficiency")
FLOORS = {s: Decimal("0.05") for s in SLOTS}


def _pin(monkeypatch: Any, text: str = SET) -> None:
    import hashlib

    ev = require("reader.obs.eval")
    monkeypatch.setitem(ev.EVAL_SETS["brain-eval-v1"], "sha256", hashlib.sha256(text.encode()).hexdigest())


def test_an_unpinned_set_is_refused_while_the_pin_is_null() -> None:
    ev = require("reader.obs.eval")
    assert ev.EVAL_SETS["brain-eval-v1"]["sha256"] is None  # pinned when the owner accepts V.kl-eval-set
    with pytest.raises(ev.StartRefused) as refused:
        ev.check_start("mcp", SLOTS, set_text=SET, cap_usd=Decimal(5), floors=FLOORS)
    assert refused.value.code == "set_not_pinned"


@pytest.mark.parametrize(("profile", "judges", "cap", "code"), [
    ("web", SLOTS, Decimal(5), "bad_request"),
    ("mcp", ("judge.eval.vibes",), Decimal(5), "bad_request"),
    ("mcp", ("judge.find",), Decimal(5), "bad_request"),  # only judge.eval.* slots score an evaluation
    ("mcp", SLOTS, None, "cap_missing"),
    ("mcp", (*SLOTS, "judge.eval.groundedness"), Decimal(5), "cap_missing"),  # a slot without a floor
    ("mcp", SLOTS, Decimal("0.09"), "eval_cap"),  # 1 record, 2 judges at 0.05 each: 0.10 > 0.09
])
def test_start_refusals(monkeypatch: Any, profile: str, judges: Any, cap: Any, code: str) -> None:
    ev = require("reader.obs.eval")
    _pin(monkeypatch)
    with pytest.raises(ev.StartRefused) as refused:
        ev.check_start(profile, judges, set_text=SET, cap_usd=cap, floors=FLOORS)
    assert refused.value.code == code


def test_start_accepts_a_pinned_set_inside_the_cap(monkeypatch: Any) -> None:
    ev = require("reader.obs.eval")
    _pin(monkeypatch)
    name, records = ev.check_start("tutor", SLOTS, set_text=SET, cap_usd=Decimal("0.10"), floors=FLOORS)
    assert name == "brain-eval-v1" and [r["id"] for r in records] == ["e01"]


def _record(qtype: str, find_ms: int, tokens: int, judge_ran: bool | None, gate: str, state: str = "done") -> Any:
    ev = require("reader.obs.eval")
    return ev.RecordRow(question_type=qtype, find_ms=find_ms, tokens=tokens, judge_ran=judge_ran, gate=gate,
                        state=state, skipped_not_entitled=1, invalid_outputs=0)


def test_metrics_from_rows() -> None:
    ev = require("reader.obs.eval")
    records = [
        _record("exact", 100, 1000, True, "not_separated"),
        _record("exact", 300, 2000, False, "not_separated"),
        _record("thematic", 200, 1500, None, "separated"),  # the gate separated: not in the completion share
        _record("fact_check", 0, 0, None, "", state="failed"),
    ]
    scores = {"judge.eval.sufficiency": [("exact", 0.8), ("exact", 0.4), ("thematic", 0.9)],
              "judge.eval.relevance": [("exact", 0.5)]}
    m = ev.metrics(records, scores)
    assert m["records"] == 4 and m["failed"] == 1 and m["capped"] == 0
    assert m["find_ms.p50"] == 200.0 and m["find_ms.p95"] == 300.0  # nearest rank over done records
    assert m["tokens.mean"] == 1500.0 and m["tokens.p95"] == 2000.0
    assert m["judged_completion"] == 0.5  # 1 judged of 2 whose gate did not separate
    assert m["calls_per_answer"] == 1.0  # caller none: the question is the single find
    assert m["mean.sufficiency"] == pytest.approx(0.7) and m["mean.relevance"] == 0.5
    assert m["mean.sufficiency.exact"] == pytest.approx(0.6) and m["mean.sufficiency.thematic"] == 0.9
    assert m["records.exact"] == 2 and m["records.fact_check"] == 1
    assert "mean.sufficiency.fact_check" not in m and "mean.groundedness" not in m  # no score, no metric
    assert m["skipped_not_entitled"] == 4


def test_the_run_snapshot_passes_vec_obs_5() -> None:
    ev = require("reader.obs.eval")
    runs = require("reader.obs.runs")
    snap = ev.snapshot(
        eval_id="20000000-0000-4000-8000-0000000000aa", state="done", start_ms=1, end_ms=2,
        params={"set_sha256": "a" * 64, "budget_profile": "mcp", "caller": "none",
                "judge.sufficiency": "judge.eval.sufficiency@" + "b" * 12},
        metrics={"records": 1, "mean.sufficiency": 0.5}, snapshot_at="2026-11-26T12:00:00Z",
    )
    assert snap.kind == "eval" and runs.ID_TAG["eval"] == "reader.eval_id"
    runs._validate(snap)
    with pytest.raises(ValueError):
        runs._validate(ev.snapshot(eval_id=snap.key, state="done", start_ms=1, end_ms=2,
                                   params={"free_text": "x"}, metrics={}, snapshot_at=snap.snapshot_at))


def test_dataset_records_never_invent_a_chapter_or_gold_pages() -> None:
    ev = require("reader.obs.eval")
    doc = {"questions": [
        {"id": "e01", "question": "q one", "question_type": "exact", "chapter": 3, "gold_pages": [10, 11],
         "expected_facts": ["FACT-CANARY"]},
        {"id": "e02", "question": "q two", "question_type": "thematic"},  # no chapter, no pages
        {"id": "e03", "question": "q three", "question_type": "fact_check", "chapter": "3",
         "gold_pages": ["10"]},  # strings (the pilot set's form): not numbers, so not sent
    ]}
    got = ev.dataset_records(doc)
    assert got[0] == {"inputs": {"question_id": "e01", "question": "q one", "question_type": "exact",
                                 "chapter": 3}, "expectations": {"gold_pages": [10, 11]}}
    assert got[1] == {"inputs": {"question_id": "e02", "question": "q two", "question_type": "thematic"}}
    assert got[2] == {"inputs": {"question_id": "e03", "question": "q three", "question_type": "fact_check"}}
    assert "FACT-CANARY" not in repr(got)
