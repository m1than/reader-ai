"""SRV-TUTOR [db], revision 2026-11-23 after the first live run, and the ADR-0039 revision (V.kl-tutor-find): a
find response sends every L1 card's span, then each card's next span (deduplicated, one alias each), so a fact one
span beside a card can be cited; GET reports a question's usage.

The world's spans are seq 0, 1 and 2; a fake find returns cards (seq, next seq).

@trace FR-TUTOR-2 FR-TUTOR-5 FR-TUTOR-6 FR-TUTOR-8
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from .conftest import (
    MAT,
    SPANS,
    FakeFind,
    FakeJobs,
    FakeProvider,
    Pg,
    Reply,
    answer_json,
    client,
    deps,
    execute,
    insert_question,
    locators,
    question_row,
    run_handler,
    world,
)

pytestmark = pytest.mark.anyio


async def test_a_card_sends_its_next_span_after_the_primaries(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    qid = await insert_question(w)
    # The quote is only in seq 1, the next span of the card for seq 0: s3 after the primaries 0 and 2.
    blocks = [{"text": "The target moves.", "spans": ["s3"], "quote": "The target is adjusted every 2016 blocks"}]
    provider = FakeProvider([Reply(text=answer_json(blocks))])
    await run_handler(w, qid, deps(w, provider, FakeFind(cards=[(0, 1), (2, None)])))
    async with w.engine.connect() as conn:
        ids = await locators(conn, w.users["ivan"], MAT)
    sent = json.loads(provider.calls[0].prompt)["sources"]
    assert [s["id"] for s in sent] == ["s1", "s2", "s3"]
    assert [s["text"] for s in sent] == [SPANS[0][1], SPANS[2][1], SPANS[1][1]]
    assert [s["page"] for s in sent] == [SPANS[0][0], SPANS[2][0], SPANS[1][0]]
    state, stored, grounded, _, _ = await question_row(w, qid)
    assert (state, grounded) == ("answered", True)
    assert [(c["span"], c["page"], c["material_title"]) for c in stored["blocks"][0]["citations"]] == [
        (ids[1], SPANS[1][0], "Book of ivan")]


async def test_spans_shared_by_cards_are_sent_once_primaries_first(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    qid = await insert_question(w)
    provider = FakeProvider()
    await run_handler(w, qid, deps(w, provider, FakeFind(cards=[(1, 2), (2, None), (0, 1)])))
    sent = json.loads(provider.calls[0].prompt)["sources"]
    # primaries 1, 2, 0 in response order; the next spans (2 and 1) were already taken
    assert [s["text"] for s in sent] == [SPANS[1][1], SPANS[2][1], SPANS[0][1]]
    assert [s["id"] for s in sent] == ["s1", "s2", "s3"]


async def test_a_citation_of_a_dropped_alias_is_removed(
    pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The budget drops the last candidate (s3); a model citing s3 anyway gets no citation and no support."""
    from reader.tutor import answer as answer_module
    from reader.tutor import params

    w = await world(pg, tmp_path)
    qid = await insert_question(w)
    texts = [SPANS[0][1], SPANS[2][1], SPANS[1][1]]
    spans = [answer_module.Span(f"{MAT}:{i}", 60, t) for i, t in zip((0, 2, 1), texts, strict=True)]
    two = len(answer_module.build_prompt("Why do miners vary the nonce?", None, spans[:2]).text.encode())
    monkeypatch.setattr(params, "PROMPT_BYTES_MAX", two)  # room for exactly two sources
    blocks = [{"text": "About the target.", "spans": ["s3"], "quote": "The target is adjusted every 2016 blocks"}]
    provider = FakeProvider([Reply(text=answer_json(blocks))])
    await run_handler(w, qid, deps(w, provider, FakeFind(cards=[(0, 1), (2, None)])))
    assert [s["id"] for s in json.loads(provider.calls[0].prompt)["sources"]] == ["s1", "s2"]
    state, stored, grounded, _, _ = await question_row(w, qid)
    assert (state, grounded) == ("answered", False)
    assert stored["blocks"] == [
        {"text": "About the target.", "supported": False, "quote": blocks[0]["quote"], "citations": []}
    ]


async def _ledger(w: object, question_id: uuid.UUID, settled: str, output: int, thinking: int | None) -> None:
    """One reservation and its settlement for the question's run, as SRV-AGENT writes them."""
    await execute(
        w.engine,  # type: ignore[attr-defined]
        "INSERT INTO agent_spend (owner_id, run_id, stage, task, attempt, kind, model, usd)"
        " VALUES (:o, :r, 'tutor', 'tutor', 1, 'reserve', 'claude-sonnet-5-5', 0.13)",
        o=w.users["ivan"], r=question_id,  # type: ignore[attr-defined]
    )
    await execute(
        w.engine,  # type: ignore[attr-defined]
        "INSERT INTO agent_spend (owner_id, run_id, stage, task, attempt, kind, reservation_id, model, usd,"
        " settled_usd, output, thinking)"
        " SELECT owner_id, run_id, stage, task, attempt, 'settle', id, model, :s, :s, :out, :th"
        " FROM agent_spend WHERE run_id = :r AND kind = 'reserve'",
        r=question_id, s=settled, out=output, th=thinking,
    )


async def test_get_reports_the_questions_usage(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    lesson = w.lessons["ivan"]
    answered = await insert_question(w)
    async with client(w, jobs=FakeJobs()) as c:
        assert (await c.get(f"/api/lessons/{lesson}/questions/{answered}")).json() == {"state": "queued"}
    await run_handler(w, answered, deps(w, FakeProvider()))
    await _ledger(w, answered, "0.041", 312, 0)
    refused = await insert_question(w, question="Another?")
    await run_handler(w, refused, deps(w, FakeProvider([Reply(code="budget_exhausted")])))
    async with client(w, jobs=FakeJobs()) as c:
        one = (await c.get(f"/api/lessons/{lesson}/questions/{answered}")).json()
        two = (await c.get(f"/api/lessons/{lesson}/questions/{refused}")).json()
    assert one["state"] == "answered"
    assert one["usage"] == {"usd": 0.041, "output_tokens": 312, "thinking_tokens": 0}
    assert two["state"] == "refused"
    assert two["usage"] == {"usd": 0.0, "output_tokens": 0, "thinking_tokens": None}
