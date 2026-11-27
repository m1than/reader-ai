"""Re-anchoring between two packs of one material (SRV-INGEST [db], VEC-INGEST-11).

@trace FR-INGEST-43 VEC-INGEST-11 INV-INGEST-15
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from .conftest import FIXTURES, Pg, need, world
from .test_seqmap_db import R2, R3, ingest_of, sha_of, synthetic_pack

pytestmark = pytest.mark.anyio
CASES = YAML(typ="safe").load((FIXTURES / "reanchor-cases.yaml").read_text(encoding="utf-8"))["cases"]


def rows_of(pages: dict[int, list[str]]) -> list[tuple[int, str]]:
    return [(page, body) for page in sorted(pages) for body in pages[page]]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
async def test_reanchor_cases(pg: Pg, tmp_path: Path, case: dict[str, Any]) -> None:
    reanchor = need("reader.ingest.reanchor")
    async with world(pg, tmp_path) as w:
        owner = w.users["ivan"]
        sha = sha_of(case["name"])
        old = await synthetic_pack(w, sha, R2, rows_of(case["old"]))
        new = await synthetic_pack(w, sha, R3, rows_of(case["new"]))
        span_sql = "SELECT locator, quote_sha256 FROM material_spans WHERE ingest_id = :i ORDER BY seq"
        old_spans = await w.rows(span_sql, i=await ingest_of(w, old))
        new_spans = await w.rows(span_sql, i=await ingest_of(w, new))
        async with w.engine.begin() as conn:
            counts = await reanchor.run(conn, owner, old, new)
        expected = [(o[0], o[1], None, "orphaned") if i is None else (o[0], o[1], new_spans[i][0], "mapped")
                    for o, i in zip(old_spans, case["expect"], strict=True)]
        got = await w.rows("SELECT from_locator, quote_sha256, to_locator, state FROM anchor_maps"
                           " WHERE from_pack_id = :f AND to_pack_id = :t", f=old, t=new)
        assert sorted(tuple(r) for r in got) == sorted(expected)
        mapped = sum(i is not None for i in case["expect"])
        assert counts == (mapped, len(expected) - mapped)
        run = await w.rows("SELECT mapped, orphaned FROM anchor_runs WHERE from_pack_id = :f"
                           " AND to_pack_id = :t", f=old, t=new)
        assert [tuple(r) for r in run] == [counts]
        async with w.engine.begin() as conn:  # an existing run is reused
            assert await reanchor.run(conn, owner, old, new) == counts
        assert await w.scalar("SELECT count(*) FROM anchor_runs") == 1
        assert await w.scalar("SELECT count(*) FROM anchor_maps") == len(expected)


async def test_the_job_ends_with_try_activate(pg: Pg, tmp_path: Path) -> None:
    from .test_overlays_db import activate, vectors

    manifest = need("reader.ingest.manifest")
    async with world(pg, tmp_path) as w:
        owner = w.users["ivan"]
        sha = sha_of("job")
        old = await synthetic_pack(w, sha, R2, [(1, "a"), (1, "b")])
        await activate(w, old)
        new = await synthetic_pack(w, sha, R3, [(1, "a"), (1, "b")])
        async with w.engine.begin() as conn:
            built = await need("reader.ingest.seqmap").build(conn, owner, await ingest_of(w, old), new)
            assert built == "accepted"
            v3 = await vectors(conn, owner, new)
            await manifest.request_activation(conn, owner, new, {"vectors": v3})
            assert await manifest.try_activate(conn, owner, new) == "anchors_missing"  # enqueues the job
        await w.work()
        assert await w.scalar("SELECT mapped FROM anchor_runs") == 2
        async with w.engine.connect() as conn:
            view = await manifest.active_view(conn, owner)
        assert [e["pack_id"] for e in view.entry["packs"]] == [new]
        assert await w.scalar("SELECT count(*) FROM pending_activations") == 0
