"""SRV-TUTOR [db] FR-TUTOR-22: `reader.tutor.citations.REWRITERS` declares `tutor_questions.answer` as a target
of SRV-INGEST's citation rewrite (FR-INGEST-41): legacy `<sha>:<seq>` citations of answered rows become the
accepted seq map's locators with their quote hash; cleared rows are skipped; a second run changes nothing.

@trace FR-TUTOR-22
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ingest.packseed import R3, add_pack

from .conftest import MAT, Pg, execute, insert_question, need, scalar, world

pytestmark = pytest.mark.anyio
R3_SPANS = [(61, "text", "Re-converted: the nonce varies."), (62, "text", "Re-converted: the target moves.")]


def _answer(span: str) -> dict[str, Any]:
    return {"blocks": [{"text": "x", "supported": True, "quote": "q",
                        "citations": [{"span": span, "page": 61, "material_title": "Book of ivan"}]}],
            "insufficient": False, "retrieval": "ok", "prompt_sha256": "0" * 64, "model": "claude-sonnet-5-5"}


async def test_answered_rows_are_rewritten_and_cleared_rows_skipped(pg: Pg, tmp_path: Path) -> None:
    from sqlalchemy import text

    citations = need("reader.tutor.citations")
    migrate = need("reader.ingest.migrate_citations")
    locator = need("reader.ingest.locator")
    views = need("reader.ingest.views")
    store = need("reader.tutor.store")
    w = await world(pg, tmp_path)
    ivan = w.users["ivan"]
    (target,) = citations.REWRITERS
    assert isinstance(target, migrate.Target)
    assert migrate.targets() == [target]  # the module exists, so the command finds the declaration

    legacy = f"{MAT}:0"
    answered = await insert_question(w)
    await execute(w.engine, "UPDATE tutor_questions SET state = 'answered', answer = CAST(:a AS jsonb),"
                  " grounded = true, finished_at = now() WHERE id = :i", a=json.dumps(_answer(legacy)), i=answered)
    cleared = await insert_question(w, question="Second?")
    await execute(w.engine, "UPDATE tutor_questions SET state = 'failed', error_code = 'tutor_failed',"
                  " finished_at = now() WHERE id = :i", i=cleared)
    await execute(w.engine, "UPDATE tutor_questions SET question = '', answer = NULL, cleared_at = now()"
                  " WHERE id = :i", i=cleared)

    r3_loc = locator.page_locators(MAT, 61, [R3_SPANS[0][2]])[0]
    r3_hash = locator.quote_sha256(R3_SPANS[0][2])
    async with w.engine.begin() as conn:
        r2_ingest = (await views.active_pack(conn, ivan, MAT)).ingest_id
        r3 = await add_pack(conn, ivan, MAT, R3, R3_SPANS, activate=True)
        await conn.execute(text(
            "INSERT INTO seq_maps (owner_id, from_ingest_id, to_pack_id, state, span_count)"
            " VALUES (:o, :i, :p, 'accepted', 3)"), {"o": ivan, "i": r2_ingest, "p": r3})
        await conn.execute(text(
            "INSERT INTO span_seq_map (owner_id, from_ingest_id, to_pack_id, seq, locator, quote_sha256)"
            " VALUES (:o, :i, :p, 0, :l, :q)"), {"o": ivan, "i": r2_ingest, "p": r3, "l": r3_loc, "q": r3_hash})

    # Never run: the tutor's span load answers the legacy id and its locator alike (the resolver, FR-INGEST-42).
    async with w.engine.connect() as conn:
        loaded = await store.spans_by_ids(conn, ivan, [legacy, r3_loc])
    assert loaded[legacy] == loaded[r3_loc] == (61, R3_SPANS[0][2], r3_hash)

    assert await migrate.run(w.engine, [target]) == (0, {"result": "migrated", "rows": {target.name: 1}})
    stored = await scalar(w.engine, "SELECT answer FROM tutor_questions WHERE id = :i", i=answered)
    assert stored["blocks"][0]["citations"] == [
        {"span": r3_loc, "quote_sha256": r3_hash, "page": 61, "material_title": "Book of ivan"}]
    assert await scalar(w.engine, "SELECT answer FROM tutor_questions WHERE id = :i", i=cleared) is None
    assert await migrate.run(w.engine, [target]) == (0, {"result": "migrated", "rows": {target.name: 0}})
    again = await scalar(w.engine, "SELECT answer FROM tutor_questions WHERE id = :i", i=answered)
    assert again == stored
