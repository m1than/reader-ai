"""SRV-SEARCH [db]: entitlement over material rows, the B8 per-channel table (VEC-SEARCH-9), quotes and the
per-material quote allowance over the active pack.

@trace FR-SEARCH-30
@trace VEC-SEARCH-9
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from .conftest import MAT_EN, MAT_RU, Pg, World, add_material, fixture, need, set_profile, world

pytestmark = pytest.mark.anyio

LONG = "alpha " * 300  # 1,800 characters: over the L1 card budget of 1,200 characters
SPANS = [(1, "Opening words before any section."), (2, LONG), (3, "Short span.")]
OUTLINE = [(1, "Part", 2), (2, "Chapter", 3)]


@pytest.fixture
async def w(pg: Pg, tmp_path: Path) -> AsyncIterator[World]:
    built = await world(pg, tmp_path)
    try:
        yield built
    finally:
        await built.engine.dispose()


async def book_of(w: World, handle: str, sha: str) -> Any:
    opening = need("reader.search.opening")
    policy = need("reader.search.policy").policy()
    from reader.ingest import views

    async with w.engine.connect() as conn:
        pack = await views.active_pack(conn, w.users[handle], sha)
        assert pack is not None
        return await opening.load_book(conn, pack, policy)


async def is_entitled(w: World, handle: str, owner: str, sha: str) -> bool:
    opening = need("reader.search.opening")
    from reader.ingest import views

    async with w.engine.connect() as conn:
        pack = await views.active_pack(conn, w.users[owner], sha)
        assert pack is not None
        result: bool = await opening.entitled(conn, w.users[handle], pack)
        return result


async def test_entitled_for_the_owner_and_an_own_live_copy_only(w: World) -> None:
    await add_material(w, "ivan", MAT_RU, SPANS)
    assert await is_entitled(w, "ivan", "ivan", MAT_RU)
    assert not await is_entitled(w, "anna", "ivan", MAT_RU)
    # an uploads row alone (a sha256 the client named, never received as a material) does not count
    async with w.engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO uploads (owner_id, sha256, size, filename, language, state)"
            " VALUES (:o, :s, 1, 'x.pdf', 'ru', 'failed')"), {"o": w.users["anna"], "s": MAT_RU})
    assert not await is_entitled(w, "anna", "ivan", MAT_RU)
    await add_material(w, "anna", MAT_RU, SPANS)  # her own copy, received from her bytes
    assert await is_entitled(w, "anna", "ivan", MAT_RU)
    async with w.engine.begin() as conn:
        await conn.execute(
            text("UPDATE materials SET deleted_at = now() WHERE owner_id = :o AND sha256 = :s"),
            {"o": w.users["anna"], "s": MAT_RU})
    assert not await is_entitled(w, "anna", "ivan", MAT_RU)


@pytest.mark.parametrize("row", fixture("personal-use.yaml")["table"],
                         ids=lambda r: f"{r['channel']}-{r['entitled']}-{r['license']}")
async def test_vec_search_9_channel_table(w: World, row: dict[str, Any]) -> None:
    await add_material(w, "ivan", MAT_EN, SPANS, config_id="book-en", outline=OUTLINE)
    await set_profile(w, "ivan", MAT_EN, row["license"])
    book = await book_of(w, "ivan", MAT_EN)
    channel, entitled = row["channel"], row["entitled"]
    span_ids = [s.locator for s in book.spans]
    l1 = [book.card(i, channel, entitled) for i in span_ids]
    if row["l1"] == "none":
        assert l1 == [None, None, None]
    else:
        long_card = l1[1]
        assert long_card is not None and long_card.layer == "L1"
        if row["l1"] == "full":
            assert long_card.text == LONG and not long_card.more
        else:
            assert len(long_card.text) <= 1200 and LONG.startswith(long_card.text)
            assert long_card.more == (row["license"] == "shareable")
    linked = set()
    for card_id in [book.source.id, *(n.id for n in book.nodes)]:
        card = book.card(card_id, channel, entitled)
        assert card is not None
        linked |= {x for x in card.next if x.startswith("spn:")}
        linked |= {x for x in [c.id for c in book.descendants(card_id, 4, channel, entitled)]
                   if x.startswith("spn:")}
    assert bool(linked) == (row["l1"] != "none")


@pytest.mark.parametrize("case", fixture("personal-use.yaml")["l2_cut"], ids=lambda c: c["case"])
def test_vec_search_9_l2_cut_order(case: dict[str, Any]) -> None:
    cards = need("reader.search.cards")
    got = cards.l2_text(case["title"], case["summary"], case["quote"], case["tokens"])
    assert got == (case["text"], case["quote_served"])


@pytest.mark.parametrize("case", fixture("personal-use.yaml")["allowance"], ids=lambda c: c["case"])
def test_vec_search_9_allowance_walk(case: dict[str, Any]) -> None:
    cards = need("reader.search.cards")
    nodes = [(n[0], n[1], n[2]) for n in case["nodes"]]
    assert cards.allowance(nodes, case["total"], case["fraction"]) == frozenset(case["granted"])


def test_quote_is_the_first_300_code_points_cut_at_whitespace_or_hard() -> None:
    cards = need("reader.search.cards")
    spaced = "word " * 100
    assert cards.quote(spaced, 300) == spaced[:299]
    assert cards.quote("x" * 400, 300) == "x" * 300
    assert cards.quote("short", 300) == "short"


async def test_l2_quote_fits_the_l2_text_budget_and_past_allowance_nodes_carry_no_quote(w: World) -> None:
    sections = [(p, f"Section {p} " + "body " * 70) for p in range(1, 30)]
    await add_material(w, "ivan", MAT_EN, sections, config_id="book-en",
                       outline=[(1, f"Section {p}", p) for p in range(1, 30)])
    book = await book_of(w, "ivan", MAT_EN)
    cards = need("reader.search.cards")
    seen_granted = seen_refused = False
    for i, node in enumerate(book.nodes):
        card = book.node_card(i, "mcp", True)
        budget = cards.l2_budget(book.policy, has_children=False, top_level=True)
        assert len(card.cite) == 1
        cite = card.cite[0]
        span = book.spans[book.span_index[cite.spn]]
        assert cite.quote_sha256 == span.quote_sha256
        if node.id in book.granted:
            seen_granted = True
            assert cite.quote is not None and span.text.startswith(cite.quote)
            assert len(card.text) + len(cite.quote) <= cards.chars(budget)
        else:
            seen_refused = True
            assert cite.quote is None
    assert seen_granted and seen_refused


async def test_allowance_is_over_the_active_pack_only(w: World) -> None:
    opening = need("reader.search.opening")
    await add_material(w, "ivan", MAT_EN, SPANS, config_id="book-en", outline=OUTLINE)
    async with w.engine.connect() as conn:
        first = await opening.quote_allowance(conn, w.users["ivan"], MAT_EN)
        again = await opening.quote_allowance(conn, w.users["ivan"], MAT_EN)
        assert first is not None and first == again
        # another owner, and a material without an active pack, have no allowance
        assert await opening.quote_allowance(conn, w.users["anna"], MAT_EN) is None
        assert await opening.quote_allowance(conn, w.users["ivan"], MAT_RU) is None
