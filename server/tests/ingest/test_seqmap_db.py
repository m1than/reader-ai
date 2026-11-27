"""The seq-to-locator map of the legacy ingest (SRV-INGEST [db], VEC-INGEST-10).

@trace FR-INGEST-40 VEC-INGEST-10 INV-INGEST-15
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from .conftest import FIXTURES, Pg, World, build_artifact, need, world

pytestmark = pytest.mark.anyio
R2 = "docling-2.151.1+r2"
R3 = "docling-2.151.1+r3"
CASES = YAML(typ="safe").load((FIXTURES / "seqmap-cases.yaml").read_text(encoding="utf-8"))["cases"]


def sha_of(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


def spans_of(rows: Sequence[Sequence[Any]]) -> list[dict[str, Any]]:
    """[page, kind, text] rows (or (page, text) with the kind derived) as artifact span lines."""
    out = []
    for seq, row in enumerate(rows):
        page, kind, body = row if len(row) == 3 else (row[0], "figure" if row[1] == "" else "text", row[1])
        out.append({"seq": seq, "page": page, "kind": kind, "level": 1 if kind == "heading" else None,
                    "decoded": None, "text": body, "bbox": [72.0, 60.0, 300.0, 80.0]})
    return out


async def synthetic_pack(w: World, sha: str, version: str, rows: Sequence[Sequence[Any]]) -> str:
    """A sealed pack of `sha` under `version` holding exactly `rows` (import_rows, bypassing receive)."""
    files = build_artifact(spans=spans_of(rows), material_sha256=sha, converter_version=version)
    async with w.engine.begin() as conn:
        assert await need("reader.ingest.store").import_rows(
            conn, w.users["ivan"], sha[:12], "e" * 64, json.loads(files["manifest.json"]),
            files["spans.jsonl"], json.loads(files["outline.json"]))
    return str(need("reader.ingest.packs").pack_id(sha, version))


async def ingest_of(w: World, pack: str) -> Any:
    return await w.scalar("SELECT ingest_id FROM base_packs WHERE pack_id = :p", p=pack)


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
async def test_seqmap_cases(pg: Pg, tmp_path: Path, case: dict[str, Any]) -> None:
    seqmap = need("reader.ingest.seqmap")
    async with world(pg, tmp_path) as w:
        owner = w.users["ivan"]
        sha = sha_of(case["name"])
        old = await synthetic_pack(w, sha, R2, case["old"])
        new = await synthetic_pack(w, sha, R3, case["new"])
        legacy = await ingest_of(w, old)
        async with w.engine.begin() as conn:
            outcome = await seqmap.build(conn, owner, legacy, new)
        rows = await w.rows("SELECT state, reason, span_count FROM seq_maps")
        mapped = await w.rows("SELECT seq, locator, quote_sha256 FROM span_seq_map ORDER BY seq")
        if case["expect"] == "accepted":
            assert outcome == "accepted"
            assert [tuple(r) for r in rows] == [("accepted", None, len(case["old"]))]
            expected = await w.rows("SELECT s.seq, s.locator, s.quote_sha256 FROM material_spans s"
                                    " WHERE s.ingest_id = :i ORDER BY s.seq", i=await ingest_of(w, new))
            assert [tuple(r) for r in mapped] == [tuple(r) for r in expected]
        else:
            reason = f"{case['expect']['reason']}:{case['expect']['seq']}"
            assert outcome == f"refused:{reason}"
            assert [tuple(r) for r in rows] == [("refused", reason, len(case["old"]))]
            assert mapped == []
            assert await w.scalar("SELECT count(*) FROM manifest_entries") == 0
        async with w.engine.begin() as conn:  # a second run changes nothing
            assert await seqmap.build(conn, owner, legacy, new) == "exists"
        assert await w.scalar("SELECT count(*) FROM seq_maps") == 1


async def test_the_job_is_declared_with_its_key(pg: Pg, tmp_path: Path) -> None:
    jobs = need("reader.ingest.jobs")
    kinds = {k.name: k for k in jobs.make_kinds(tmp_path)}
    assert kinds["ingest.seqmap"].key == ("owner_id", "from_ingest_id", "to_pack_id")
    assert kinds["ingest.reanchor"].key == ("owner_id", "from_pack_id", "to_pack_id")
    async with world(pg, tmp_path) as w:  # the import of an r3 pack enqueues the map while none is accepted
        sha = sha_of("job")
        await synthetic_pack(w, sha, R2, [(1, "a")])
        new = await synthetic_pack(w, sha, R3, [(1, "a")])
        async with w.engine.begin() as conn:
            assert await jobs.enqueue_followups(conn, w.users["ivan"], new) == ["ingest.seqmap"]
        await w.work()
        assert await w.scalar("SELECT state FROM seq_maps") == "accepted"
        async with w.engine.begin() as conn:  # an accepted map exists: nothing more
            assert await jobs.enqueue_followups(conn, w.users["ivan"], new) == []


def _without(w: World, *names: str) -> Any:
    from reader.jobs import Catalog

    full = w.jobs.catalog
    w.jobs.catalog = Catalog([full[n] for n in full if n not in names])
    return full


async def test_a_lost_followup_is_recovered_by_the_sweep(pg: Pg, tmp_path: Path, caplog: Any) -> None:
    """Code review of unit A, blocking 4: an unwired kind logs and enqueues nothing; a try_activate that
    stores seqmap_missing enqueues the map, so the worker-start sweep recovers a lost follow-up."""
    import logging

    from .test_overlays_db import activate, vectors

    jobs, manifest = need("reader.ingest.jobs"), need("reader.ingest.manifest")
    caplog.set_level(logging.INFO)
    async with world(pg, tmp_path) as w:
        owner = w.users["ivan"]
        sha = sha_of("lost")
        legacy = await synthetic_pack(w, sha, R2, [(1, "a")])
        await activate(w, legacy)
        new = await synthetic_pack(w, sha, R3, [(1, "a")])
        full = _without(w, "ingest.seqmap", "ingest.reanchor")
        async with w.engine.begin() as conn:
            assert await jobs.enqueue_followups(conn, owner, new) == []
            v = await vectors(conn, owner, new)
            await manifest.request_activation(conn, owner, new, {"vectors": v})
            assert await manifest.try_activate(conn, owner, new) == "seqmap_missing"
        assert "ingest kind_unwired ingest.seqmap" in caplog.text
        assert await w.scalar("SELECT count(*) FROM job_keys WHERE kind = 'ingest.seqmap'") == 0
        w.jobs.catalog = full  # the paired wiring lands; the worker restarts
        await manifest.sweep(w.engine)
        assert await w.scalar("SELECT count(*) FROM job_keys WHERE kind = 'ingest.seqmap'") == 1
        await w.work()
        assert await w.scalar("SELECT state FROM seq_maps") == "accepted"
        assert await w.scalar("SELECT count(*) FROM pending_activations WHERE pack_id = :p", p=new) == 0


async def test_after_end_of_an_import_enqueues_the_map(pg: Pg, tmp_path: Path) -> None:
    jobs = need("reader.ingest.jobs")
    async with world(pg, tmp_path) as w:
        sha = sha_of("after")
        await synthetic_pack(w, sha, R2, [(1, "a")])
        await synthetic_pack(w, sha, R3, [(1, "a")])
        payload = {"material_sha256": sha, "converter_version": R3, "artifact_id": "e" * 64,
                   "material_id": sha[:12]}
        await jobs.after_end(w.engine, payload, "dead")
        assert await w.scalar("SELECT count(*) FROM job_keys WHERE kind = 'ingest.seqmap'") == 0
        await jobs.after_end(w.engine, payload, "succeeded")
        assert await w.scalar("SELECT count(*) FROM job_keys WHERE kind = 'ingest.seqmap'") == 1


async def test_build_refuses_an_ingest_that_is_not_the_materials_legacy(pg: Pg, tmp_path: Path) -> None:
    seqmap = need("reader.ingest.seqmap")
    async with world(pg, tmp_path) as w:
        owner = w.users["ivan"]
        s1, s2 = sha_of("m1"), sha_of("m2")
        l1 = await synthetic_pack(w, s1, R2, [(1, "a")])
        await synthetic_pack(w, s2, R2, [(1, "a")])
        new2 = await synthetic_pack(w, s2, R3, [(1, "a")])
        new1 = await synthetic_pack(w, s1, R3, [(1, "a")])
        async with w.engine.begin() as conn:
            assert await seqmap.build(conn, owner, await ingest_of(w, l1), new2) == "not_legacy"
            assert await seqmap.build(conn, owner, await ingest_of(w, new1), new2) == "not_legacy"
        assert await w.scalar("SELECT count(*) FROM seq_maps") == 0
