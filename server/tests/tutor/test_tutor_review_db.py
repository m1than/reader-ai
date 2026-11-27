"""SRV-TUTOR [db], code review round 1: rules that mutants survived — span reads filtered by owner, the `finish`
guard, the job key through the real SRV-EVENTS `Jobs`, the insert race, and canaries on the failure paths.

@trace FR-TUTOR-3 FR-TUTOR-5 FR-TUTOR-8 FR-TUTOR-9 FR-TUTOR-10 FR-TUTOR-12 INV-TUTOR-1 INV-TUTOR-4 INV-TUTOR-6
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from .conftest import (
    SPANS,
    FakeJobs,
    FakeProvider,
    FakeSearch,
    Pg,
    Reply,
    client,
    deps,
    execute,
    insert_question,
    need,
    question_row,
    run_handler,
    world,
)

pytestmark = pytest.mark.anyio


# ---- blocker 1: span reads are filtered by owner


async def test_each_owner_sees_only_their_own_span_text_and_page(pg: Pg, tmp_path: Path) -> None:
    # anna's copy of the same material: other text and other pages at the same `sha:seq` span ids
    anna = [(page + 100, f"ANNA-ONLY {seq} miners vary the nonce until the hash")
            for seq, (page, _) in enumerate(SPANS)]
    w = await world(pg, tmp_path, {"anna": anna})
    seen: dict[str, tuple[list[str], list[int], list[int]]] = {}
    for handle in ("ivan", "anna"):
        qid = await insert_question(w, handle)
        provider = FakeProvider()
        await run_handler(w, qid, deps(w, provider))
        sent = json.loads(provider.calls[0].prompt)["sources"]
        stored = (await question_row(w, qid))[1]
        pages = [c["page"] for b in stored["blocks"] for c in b["citations"]]
        seen[handle] = ([s["text"] for s in sent], [s["page"] for s in sent], pages)
    ivan_texts, ivan_pages, ivan_cited = seen["ivan"]
    anna_texts, anna_pages, anna_cited = seen["anna"]
    assert ivan_texts == [text for _, text in SPANS]
    assert ivan_pages == [page for page, _ in SPANS]
    assert ivan_cited == [SPANS[0][0]]
    assert all(t.startswith("ANNA-ONLY") for t in anna_texts)
    assert anna_pages == [page + 100 for page, _ in SPANS]
    assert anna_cited == [SPANS[0][0] + 100]


# ---- blocker 3: the finish guard


async def test_finish_refuses_a_row_that_is_no_longer_queued(pg: Pg, tmp_path: Path) -> None:
    store = need("reader.tutor.store")
    w = await world(pg, tmp_path)
    qid = await insert_question(w)
    async with w.engine.begin() as conn:
        assert await store.finish(conn, qid, "failed", error_code="lost") is True
    before = await question_row(w, qid)
    async with w.engine.begin() as conn:
        assert await store.finish(conn, qid, "answered", answer={"blocks": []}, grounded=True, model="m") is False
    assert await question_row(w, qid) == before == ("failed", None, None, "lost", None)


@dataclass
class FlippingProvider:
    """Expires the row from another connection while the call runs (as a read past the TTL would)."""

    engine: Any
    question_id: uuid.UUID
    inner: FakeProvider = field(default_factory=FakeProvider)

    async def run(self, call: Any) -> Any:
        await execute(
            self.engine,
            "UPDATE tutor_questions SET state = 'expired', error_code = 'expired', finished_at = now() WHERE id = :i",
            i=self.question_id,
        )
        return await self.inner.run(call)


async def test_a_row_flipped_during_the_call_is_not_overwritten(
    pg: Pg, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    w = await world(pg, tmp_path)
    qid = await insert_question(w)
    with caplog.at_level(logging.INFO, logger="reader.tutor"):
        await run_handler(w, qid, deps(w, FlippingProvider(w.engine, qid)))
    assert await question_row(w, qid) == ("expired", None, None, "expired", None)
    assert any("state=answered written=False" in r.getMessage() for r in caplog.records)


# ---- blocker 5: the job key through the real SRV-EVENTS Jobs


async def test_job_key_matches_the_real_queue(pg: Pg, tmp_path: Path) -> None:
    tutor_jobs = need("reader.tutor.jobs")
    params = need("reader.tutor.params")
    from reader.config import Secret
    from reader.db import DbTarget
    from reader.jobs.catalog import Catalog
    from reader.jobs.queue import Jobs
    from reader.jobs.worker import make_app

    w = await world(pg, tmp_path)
    qid = await insert_question(w)
    app = make_app(DbTarget(pg.host, pg.port, pg.user, pg.database, Secret(pg.password)))
    await app.open_async()
    try:
        queue = Jobs(w.engine, app, Catalog(tutor_jobs.JOB_KINDS), (pg.password,))
        assert (await queue.enqueue(params.KIND, {"question_id": str(qid)})).kind == "queued"
        assert await queue.job_status(params.KIND, tutor_jobs.job_key(qid)) == "queued"
        assert await queue.job_status(params.KIND, tutor_jobs.job_key(uuid.uuid4())) == "none"
    finally:
        await app.close_async()


# ---- insert race: the partial unique index answers 409


async def test_insert_race_answers_409(pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = need("reader.tutor.store")
    w = await world(pg, tmp_path)
    await insert_question(w)  # a live question the busy check will not see (the race)

    async def nothing_queued(conn: Any, owner_id: uuid.UUID) -> list[tuple[uuid.UUID, float]]:
        return []

    monkeypatch.setattr(store, "queued_of", nothing_queued)
    jobs = FakeJobs()
    async with client(w, jobs=jobs) as c:
        r = await c.post(f"/api/lessons/{w.lessons['ivan']}/ask", json={"question": "raced"})
    assert (r.status_code, r.json()) == (409, {"error": "tutor_busy"})
    assert jobs.enqueued == []


# ---- canaries on the failure paths (INV-TUTOR-4)


async def test_no_text_in_logs_when_the_output_is_invalid(
    pg: Pg, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    w = await world(pg, tmp_path)
    canary = "CANARY-OUTPUT-3e1f"
    qid = await insert_question(w, question="Why CANARY-QUESTION-0a9b?")
    reply = Reply(text=f"Sure! {canary} is the answer.")
    with caplog.at_level(logging.DEBUG):
        await run_handler(w, qid, deps(w, FakeProvider([reply])), attempts=1)
    assert (await question_row(w, qid))[0::3] == ("failed", "output_invalid")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert canary not in text
    assert "CANARY-QUESTION-0a9b" not in text


@dataclass
class RaisingSearch(FakeSearch):
    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("search failed for CANARY-SEARCH-77c2 and CANARY-QUESTION-5d4e")


async def test_no_text_in_logs_when_search_raises(
    pg: Pg, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    w = await world(pg, tmp_path)
    qid = await insert_question(w, question="Why CANARY-QUESTION-5d4e?")
    provider = FakeProvider()
    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError):
        await run_handler(w, qid, deps(w, provider, RaisingSearch()))
    assert provider.calls == []
    assert (await question_row(w, qid))[0] == "queued"  # SRV-EVENTS judges the job; a read then finalises the row
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "CANARY" not in text
