# @trace FR-PLAN-9 FR-PLAN-13
"""The plan reads the material's active pack only (FR-PLAN-9's paired change, ADR-0039 / SRV-INGEST
INV-INGEST-16, V.kl-active-view-readers). [db]

The material holds its fixture's r2 pack and an r3 re-conversion with other page lengths and outline: pages,
outline, ingest id and a lesson's characters come from one pack, never their sum."""

from __future__ import annotations

from pathlib import Path

import pytest

from ingest.packseed import R3, activate_pack, add_pack

from .conftest import OUTLINE, PAGE_CHARS, PLAN_SHA, Pg, body, client, need, plan_env

pytestmark = pytest.mark.anyio
R3_SPANS = [(1, "text", "y" * 500), (2, "text", "y" * 700)]


async def test_two_packs_plan_from_the_active_pack_only(pg: Pg, tmp_path: Path) -> None:
    store = need("reader.plan.store")
    async with plan_env(pg, tmp_path) as env:
        ivan = env.users["ivan"]
        async with env.engine.begin() as conn:
            r3 = await add_pack(conn, ivan, PLAN_SHA, R3, R3_SPANS)
        async with env.engine.connect() as conn:
            r2_ingest, chars, spans = await store.page_chars(conn, ivan, PLAN_SHA)
            assert chars == PAGE_CHARS  # r2 alone: not 2000 + 500 on page 1
            assert spans == tuple(1 if n else 0 for n in PAGE_CHARS)
            assert await store.outline_entries(conn, ivan, r2_ingest) == OUTLINE

        async with client(env) as c:
            preview = (await c.post("/api/plans/preview", json=body())).json()
            confirmed = await c.post(
                "/api/plans/confirm", json=body(plan_sha256=preview["plan_sha256"], compile_first=False)
            )
            assert confirmed.status_code == 200, confirmed.text
        stored = await env.scalar("SELECT ingest_id FROM study_plans WHERE owner_id = :o", o=ivan)
        assert stored == r2_ingest
        lesson = await env.scalar(
            "SELECT lesson_id FROM course_plans WHERE owner_id = :o AND pdf_first = 1", o=ivan
        )
        last = int(await env.scalar("SELECT pdf_last FROM course_plans WHERE lesson_id = :l", l=lesson))
        async with env.engine.connect() as conn:
            assert await store.lesson_chars(conn, ivan, lesson) == sum(PAGE_CHARS[:last])

        async with env.engine.begin() as conn:
            await activate_pack(conn, ivan, r3)
        async with env.engine.connect() as conn:
            r3_ingest, chars, _ = await store.page_chars(conn, ivan, PLAN_SHA)
            assert r3_ingest != r2_ingest
            assert chars[:3] == (500, 700, 0)
            assert await store.outline_entries(conn, ivan, r3_ingest) == ()  # the r3 pack has no outline rows
            assert await store.lesson_chars(conn, ivan, lesson) == sum(n for p, _, t in R3_SPANS
                                                                       if p <= last for n in [len(t)])


async def test_no_active_pack_is_not_imported(pg: Pg, tmp_path: Path) -> None:
    store = need("reader.plan.store")
    manifest = need("reader.ingest.manifest")
    async with plan_env(pg, tmp_path) as env:
        ivan = env.users["ivan"]
        async with env.engine.begin() as conn:
            seq, _ = await manifest.head(conn, ivan)
            await manifest.append(conn, ivan, seq, manifest.make_entry({}), "test_remove")
        async with env.engine.connect() as conn:
            assert await store.page_chars(conn, ivan, PLAN_SHA) is None
        async with client(env) as c:
            assert (await c.post("/api/plans/preview", json=body())).status_code == 404
