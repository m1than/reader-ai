"""Migration step 4: the one-shot rewrite of mutable legacy citations (SRV-INGEST [db]).

@trace FR-INGEST-41 INV-INGEST-15
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from .conftest import CANARY, Pg, World, need, world
from .test_overlays_db import activate
from .test_seqmap_db import R2, R3, ingest_of, sha_of, synthetic_pack

pytestmark = pytest.mark.anyio


def probe_target() -> Any:
    """A test double of a declared target (SRV-TUTOR declares `tutor_questions.answer` the same way)."""
    migrate = need("reader.ingest.migrate_citations")
    return migrate.Target(
        name="cite_probe.answer",
        select_sql="SELECT id, owner_id, answer FROM cite_probe WHERE answer IS NOT NULL"
                   " ORDER BY id FOR UPDATE",
        update_sql="UPDATE cite_probe SET answer = CAST(:doc AS jsonb) WHERE id = :key",
    )


async def put(w: World, key: int, citations: list[dict[str, Any]] | None) -> None:
    async with w.engine.begin() as conn:
        doc = None if citations is None else json.dumps({"text": "t", "citations": citations})
        await conn.execute(text(
            "INSERT INTO cite_probe (id, owner_id, answer) VALUES (:i, :o, CAST(:d AS jsonb))"),
            {"i": key, "o": w.users["ivan"], "d": doc})


async def answers(w: World) -> list[Any]:
    return [r[0] for r in await w.rows("SELECT answer FROM cite_probe ORDER BY id")]


async def test_rewrite_all_or_nothing(pg: Pg, tmp_path: Path, caplog: Any) -> None:
    migrate = need("reader.ingest.migrate_citations")
    caplog.set_level(logging.DEBUG)
    async with world(pg, tmp_path) as w:
        owner = w.users["ivan"]
        sha, other = sha_of("cite"), sha_of("unmapped")
        legacy = await synthetic_pack(w, sha, R2, [(1, CANARY), (1, "b")])
        await activate(w, legacy)
        r3 = await synthetic_pack(w, sha, R3, [(1, CANARY), (1, "b")])
        async with w.engine.begin() as conn:
            await conn.execute(text(
                "CREATE TABLE cite_probe (id int PRIMARY KEY, owner_id uuid, answer jsonb)"))
            built = await need("reader.ingest.seqmap").build(conn, owner, await ingest_of(w, legacy), r3)
            assert built == "accepted"
        located = await w.rows("SELECT locator, quote_sha256 FROM material_spans WHERE ingest_id = :i"
                               " ORDER BY seq", i=await ingest_of(w, r3))
        await put(w, 1, [{"span": f"{sha}:0", "page": 1}, {"span": f"{sha}:1", "page": 1}])
        await put(w, 2, [{"span": located[1][0], "quote_sha256": located[1][1], "page": 1}])  # located
        await put(w, 3, None)
        await put(w, 4, [{"span": f"{other}:0", "page": 1}, {"span": f"{other}:3", "page": 2}])
        before = await answers(w)

        code, reply = await migrate.run(w.engine, [probe_target()])
        assert (code, reply) == (4, {"result": "unmapped", "unmapped": {other: 2}})
        assert await answers(w) == before  # one transaction: no row changed
        assert await w.scalar("SELECT count(*) FROM citation_migrations") == 0

        async with w.engine.begin() as conn:
            await conn.execute(text("DELETE FROM cite_probe WHERE id = 4"))
        code, reply = await migrate.run(w.engine, [probe_target()])
        assert (code, reply) == (0, {"result": "migrated", "rows": {"cite_probe.answer": 1}})
        first = (await answers(w))[0]["citations"]
        assert first == [{"span": located[0][0], "quote_sha256": located[0][1], "page": 1},
                         {"span": located[1][0], "quote_sha256": located[1][1], "page": 1}]
        assert (await answers(w))[1:] == before[1:3]
        assert await migrate.run(w.engine, [probe_target()]) == (
            0, {"result": "migrated", "rows": {"cite_probe.answer": 0}})
        rows = await w.rows("SELECT target, rows FROM citation_migrations WHERE owner_id = :o"
                            " ORDER BY rows DESC", o=owner)
        assert [tuple(r) for r in rows] == [("cite_probe.answer", 1), ("cite_probe.answer", 0)]
        assert CANARY not in caplog.text and CANARY not in json.dumps(reply)


def test_the_declared_targets() -> None:
    migrate = need("reader.ingest.migrate_citations")
    assert all(isinstance(t, migrate.Target) for t in migrate.targets())
