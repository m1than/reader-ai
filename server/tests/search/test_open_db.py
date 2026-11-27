"""SRV-SEARCH [db]: `open` resolves only sealed span ids of the caller's active packs (FR-SEARCH-31,
INV-SEARCH-13).

@trace FR-SEARCH-31
@trace INV-SEARCH-13
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from .conftest import MAT_EN, MAT_RU, Pg, World, add_material, need, set_profile, world

pytestmark = pytest.mark.anyio

LONG = "beta " * 900  # 4,500 characters
SPANS = [(1, "Intro line."), (1, ""), (2, "Second page text."), (3, LONG)]
OUTLINE = [(1, "Book", 1), (2, "One", 2), (3, "Deep", 3)]


@pytest.fixture
async def w(pg: Pg, tmp_path: Path) -> AsyncIterator[World]:
    built = await world(pg, tmp_path)
    try:
        yield built
    finally:
        await built.engine.dispose()


async def call(w: World, handle: str, card_id: Any, channel: str = "owner", **kw: Any) -> dict[str, Any]:
    opening = need("reader.search.opening")
    find = need("reader.search.find")
    async with w.engine.connect() as conn:
        result: dict[str, Any] = await opening.open_(conn, find.Caller(w.users[handle], ("owner",)), channel,
                                                     card_id, **kw)
        return result


async def code(w: World, handle: str, card_id: Any, channel: str = "owner", **kw: Any) -> str:
    find = need("reader.search.find")
    with pytest.raises(find.FindError) as caught:
        await call(w, handle, card_id, channel, **kw)
    return str(caught.value.code)


async def locators(w: World, handle: str, sha: str) -> list[str]:
    from reader.ingest import views

    async with w.engine.connect() as conn:
        pack = await views.active_pack(conn, w.users[handle], sha)
        assert pack is not None
        return [s.locator for s in await views.pack_spans(conn, pack) if s.locator is not None]


async def test_a_sealed_locator_resolves_and_guesses_do_not(w: World) -> None:
    await add_material(w, "ivan", MAT_EN, SPANS, config_id="book-en", outline=OUTLINE)
    ids = await locators(w, "ivan", MAT_EN)
    got = await call(w, "ivan", ids[2])
    assert [c["id"] for c in got["cards"]] == [ids[2]] and got["cards"][0]["text"] == "Second page text."
    assert set(got) == {"brain_version", "policy_version", "budget", "cards"}
    from reader.ingest import locator

    first = locator.parse(ids[2])
    shifted = locator.pdf_locator(MAT_EN, first.page or 1, first.start + 1, first.end)
    empty = locator.parse(ids[1])
    ordinal = locator.pdf_locator(MAT_EN, 1, empty.start, empty.start, (empty.ordinal or 0) + 1)
    for guess in (shifted, ordinal, locator.pdf_locator(MAT_EN, 9, 0, 5)):
        assert await code(w, "ivan", guess) == "not_found"
    # another owner's span, and a material without an active pack for the caller
    assert await code(w, "anna", ids[2]) == "not_found"
    assert await code(w, "ivan", locator.pdf_locator(MAT_RU, 1, 0, 3)) == "not_found"


async def test_an_inactive_pack_is_not_found(w: World) -> None:
    await add_material(w, "ivan", MAT_EN, SPANS, config_id="book-en", outline=OUTLINE)
    ids = await locators(w, "ivan", MAT_EN)
    from ingest.packseed import R3, add_pack

    async with w.engine.begin() as conn:
        await add_pack(conn, w.users["ivan"], MAT_EN, R3, [(1, "text", "Other words here.")])
    from reader.ingest import locator

    inactive = locator.page_locators(MAT_EN, 1, ["Other words here."])[0]
    if inactive not in ids:
        assert await code(w, "ivan", inactive) == "not_found"
    assert (await call(w, "ivan", ids[0]))["cards"][0]["id"] == ids[0]


async def test_malformed_ids_depth_and_later_kinds(w: World) -> None:
    await add_material(w, "ivan", MAT_EN, SPANS, config_id="book-en", outline=OUTLINE)
    for bad in ("", "spn:xyz", "sec:" + MAT_EN, "src:abc", "nope:1", MAT_EN + ":0", "spn:" + MAT_EN + ":p1:3-1"):
        assert await code(w, "ivan", bad) == "id_invalid", bad
    for later in ("con:bip-39", "fnd:1", "les:1"):
        assert await code(w, "ivan", later) == "not_found"
    for depth in (0, 5, True, "2"):
        assert await code(w, "ivan", "src:" + MAT_EN, depth=depth) == "depth_invalid"
    assert await code(w, "ivan", "src:" + MAT_EN, channel="web") == "channel_invalid"
    assert await code(w, "ivan", "src:" + MAT_EN, budget=399) == "budget_invalid"


async def test_sec_children_to_depth_and_src_top_level(w: World) -> None:
    await add_material(w, "ivan", MAT_EN, SPANS, config_id="book-en", outline=OUTLINE)
    ids = await locators(w, "ivan", MAT_EN)
    book = "sec:" + MAT_EN + ":1"
    one = [c["id"] for c in (await call(w, "ivan", book, depth=1))["cards"]]
    assert one == [book, ids[0], ids[1], book + "/1"]
    two = [c["id"] for c in (await call(w, "ivan", book, depth=2))["cards"]]
    assert two == [book, ids[0], ids[1], book + "/1", ids[2], book + "/1/1"]
    top = (await call(w, "ivan", "src:" + MAT_EN))["cards"]
    assert [c["id"] for c in top] == ["src:" + MAT_EN, book] and top[0]["layer"] == "L0"
    # on mcp the personal-use pack's spans are skipped
    mcp = [c["id"] for c in (await call(w, "ivan", book, "mcp", depth=4))["cards"]]
    assert mcp == [book, book + "/1", book + "/1/1"]


async def test_budget_clamp_and_shareable_continuation(w: World) -> None:
    policy = need("reader.search.policy")
    await add_material(w, "ivan", MAT_EN, SPANS, config_id="book-en", outline=OUTLINE)
    ids = await locators(w, "ivan", MAT_EN)
    clamped = await call(w, "ivan", ids[3], budget=10**6)
    assert clamped["budget"] == 10500
    personal = clamped["cards"][0]
    assert len(personal["text"]) <= 1200 and "more" not in personal  # personal-use: cut at the card budget
    await set_profile(w, "ivan", MAT_EN, "shareable")
    small = await call(w, "ivan", ids[3], budget=600)
    card = small["cards"][0]
    assert card["more"] is True and LONG.startswith(card["text"]) and len(card["text"]) > 0
    assert policy.estimate(need("reader.search.cards").text_of(small)) <= 600
    wide = await call(w, "ivan", ids[3], budget=10500)
    assert wide["cards"][0]["text"] == LONG and "more" not in wide["cards"][0]


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(data=st.data())
async def test_open_returns_a_card_only_for_a_stored_locator(w: World, data: st.DataObject) -> None:
    from reader.ingest import locator

    if not await locators(w, "ivan", MAT_EN):
        await add_material(w, "ivan", MAT_EN, SPANS, config_id="book-en", outline=OUTLINE)
    ids = await locators(w, "ivan", MAT_EN)
    real = locator.parse(data.draw(st.sampled_from(ids)))
    start = max(0, real.start + data.draw(st.integers(-3, 3)))
    end = max(start, real.end + data.draw(st.integers(-3, 3)))
    ordinal = data.draw(st.one_of(st.none(), st.integers(0, 3))) if start == end else None
    if start == end and ordinal is None:
        ordinal = 0
    page = (real.page or 1) + data.draw(st.integers(-1, 1)) or 1
    guess = locator.pdf_locator(MAT_EN, page, start, end, ordinal)
    if guess in ids:
        assert (await call(w, "ivan", guess))["cards"][0]["id"] == guess
    else:
        assert await code(w, "ivan", guess) == "not_found"
