# @trace FR-PIPELINE-2 FR-PIPELINE-7
"""The lesson's source comes from the material's active pack only (FR-PIPELINE-2's paired change, ADR-0039 /
SRV-INGEST INV-INGEST-16, V.kl-active-view-readers). [db]

A material holds two packs (the fixture's r2 import and an r3 re-conversion): the source is never both."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ingest.packseed import R3, activate_pack, add_pack

from .conftest import MATERIAL, Pg, need, pipeline_env

pytestmark = pytest.mark.anyio
R3_SPANS = [(61, "text", "Re-converted paragraph one."), (61, "text", "Re-converted paragraph two.")]


async def test_two_packs_give_the_active_packs_spans_never_both(pg: Pg, tmp_path: Path) -> None:
    source = need("reader.pipeline.source")
    async with pipeline_env(pg, tmp_path) as env:
        owner = env.users["ivan"]
        async with env.engine.begin() as conn:
            r3 = await add_pack(conn, owner, MATERIAL, R3, R3_SPANS)
        async with env.engine.connect() as conn:
            spans, title = await source.DbSpanReader().load(conn, owner, MATERIAL, 61, 61)
        assert title == "synthetic-1"
        assert [s.span_id for s in spans] == [f"{MATERIAL}:{i}" for i in range(3)]  # r2 only, legacy ids
        assert not any(s.text.startswith("Re-converted") for s in spans)

        async with env.engine.begin() as conn:
            await activate_pack(conn, owner, r3)
        async with env.engine.connect() as conn:
            spans, _ = await source.DbSpanReader().load(conn, owner, MATERIAL, 61, 61)
        assert [s.text for s in spans] == [t for _, _, t in R3_SPANS]
        locator = need("reader.ingest.locator")
        ids = [s.span_id for s in spans]  # FR-INGEST-33 locators for the r3 pack, never `<sha>:<seq>`
        assert ids == locator.page_locators(MATERIAL, 61, [t for _, _, t in R3_SPANS])
        assert all(i.startswith(f"spn:{MATERIAL}:p61:") for i in ids)


async def test_r3_spans_carry_their_stored_quote_hashes_into_citations(pg: Pg, tmp_path: Path) -> None:
    """FR-PIPELINE-7 (r3_gates_revision): with the r3 pack active, DbSpanReader's spans carry the stored quote
    hashes, and a draft built over them cites locators with those hashes; the legacy pack's spans carry none."""
    source = need("reader.pipeline.source")
    notebook = need("reader.pipeline.notebook")
    locator = need("reader.ingest.locator")
    compact = {"cells": [{"id": "e1", "kind": "explain", "source": "x",
                          "claims": [{"id": "c1", "text": "t", "citations": [{"span": "s2"}]}]}]}

    def cite(spans: list[Any]) -> dict[str, Any]:
        built = notebook.build(compact, title="L", aliases=source.render(spans).aliases, material_title="M",
                               stage="draft", prompt_version="v1", model="m", previous=None)
        out: dict[str, Any] = built.notebook["cells"][0]["metadata"]["reader"]["claims"][0]["citations"][0]
        return out

    async with pipeline_env(pg, tmp_path) as env:
        owner = env.users["ivan"]
        async with env.engine.connect() as conn:
            legacy, _ = await source.DbSpanReader().load(conn, owner, MATERIAL, 61, 61)
        assert cite(legacy) == {"span": f"{MATERIAL}:1", "page": 61, "material_title": "M"}  # no hash
        async with env.engine.begin() as conn:
            r3 = await add_pack(conn, owner, MATERIAL, R3, R3_SPANS)
            await activate_pack(conn, owner, r3)
        async with env.engine.connect() as conn:
            spans, _ = await source.DbSpanReader().load(conn, owner, MATERIAL, 61, 61)
        assert [s.quote_sha256 for s in spans] == [locator.quote_sha256(t) for _, _, t in R3_SPANS]
        assert cite(spans) == {"span": spans[1].span_id, "quote_sha256": spans[1].quote_sha256, "page": 61,
                               "material_title": "M"}


async def test_a_material_without_an_active_pack_has_no_spans(pg: Pg, tmp_path: Path) -> None:
    source = need("reader.pipeline.source")
    async with pipeline_env(pg, tmp_path) as env:
        owner = env.users["ivan"]
        async with env.engine.begin() as conn:
            manifest = need("reader.ingest.manifest")
            seq, _ = await manifest.head(conn, owner)
            await manifest.append(conn, owner, seq, manifest.make_entry({}), "test_remove")
        async with env.engine.connect() as conn:
            spans, _ = await source.DbSpanReader().load(conn, owner, MATERIAL, 61, 62)
        assert spans == []
