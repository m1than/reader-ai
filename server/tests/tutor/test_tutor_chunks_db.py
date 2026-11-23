"""SRV-TUTOR [db], revision 2026-11-23 after the first live run: a hit sends its whole chunk (primary span first,
then the chunk's other spans, deduplicated across hits, one alias each), and GET reports a question's usage.

The world's spans are seq 0, 1 and 2; the coordinator's case "span_ids [1, 2, 3], span_id 2, quote only in span 1"
is seq [0, 1, 2], primary 1, quote only in seq 0.

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
    FakeJobs,
    FakeProvider,
    FakeSearch,
    Pg,
    Reply,
    answer_json,
    client,
    deps,
    execute,
    insert_question,
    question_row,
    run_handler,
    world,
)

pytestmark = pytest.mark.anyio


async def test_a_hit_sends_its_whole_chunk_primary_first(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    qid = await insert_question(w)
    # The quote is only in seq 0, a neighbour of the primary span: s2 after the primary-first ordering.
    blocks = [{"text": "Miners search for a nonce.", "spans": ["s2"], "quote": "Miners vary the nonce until the hash"}]
    provider = FakeProvider([Reply(text=answer_json(blocks))])
    await run_handler(w, qid, deps(w, provider, FakeSearch(chunks=[(1, [0, 1, 2])])))
    sent = json.loads(provider.calls[0].prompt)["sources"]
    assert [s["id"] for s in sent] == ["s1", "s2", "s3"]
    assert [s["text"] for s in sent] == [SPANS[1][1], SPANS[0][1], SPANS[2][1]]
    assert [s["page"] for s in sent] == [SPANS[1][0], SPANS[0][0], SPANS[2][0]]
    state, stored, grounded, _, _ = await question_row(w, qid)
    assert (state, grounded) == ("answered", True)
    assert stored["blocks"][0]["citations"] == [{"span": f"{MAT}:0", "page": SPANS[0][0], "material_title": "Book of ivan"}]


async def test_spans_shared_by_hits_are_sent_once_in_rank_order(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    qid = await insert_question(w)
    provider = FakeProvider()
    await run_handler(w, qid, deps(w, provider, FakeSearch(chunks=[(1, [0, 1]), (2, [1, 2]), (0, [0])])))
    sent = json.loads(provider.calls[0].prompt)["sources"]
    assert [s["text"] for s in sent] == [SPANS[1][1], SPANS[0][1], SPANS[2][1]]
    assert [s["id"] for s in sent] == ["s1", "s2", "s3"]


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
