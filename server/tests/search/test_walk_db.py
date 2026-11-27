"""SRV-SEARCH [db]: the personal-use walk, the cross-user case and reachability (INV-SEARCH-12).

@trace INV-SEARCH-12
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from .conftest import MAT_EN, Pg, World, add_material, need, set_profile, world

pytestmark = pytest.mark.anyio

SPANS = [(p, f"Page {p} part {k}. " + "lorem ipsum dolor " * (5 + 7 * k))
         for p in range(1, 13) for k in range(3)]
SPANS.insert(4, (2, ""))  # an empty span (a figure without a caption)
OUTLINE = [(1, "Chapter A", 1), (2, "A.1", 2), (2, "A.2", 3), (1, "Chapter B", 5), (2, "B.1", 6),
           (3, "B.1.a", 7), (1, "Chapter C", 9), (2, "C.1", 10), (2, "C.2", 11)]


@pytest.fixture
async def w(pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    built = await world(pg, tmp_path, monkeypatch)
    try:
        yield built
    finally:
        await built.engine.dispose()


async def open_(w: World, handle: str, card_id: str, channel: str) -> dict[str, Any] | str:
    opening = need("reader.search.opening")
    find = need("reader.search.find")
    async with w.engine.connect() as conn:
        try:
            result: dict[str, Any] = await opening.open_(
                conn, find.Caller(w.users[handle], ("owner",)), channel, card_id, budget=10500)
            return result
        except find.FindError as error:
            return str(error.code)


async def walk(w: World, handle: str, channel: str) -> dict[str, dict[str, Any]]:
    """Every card reachable from the source card by `open` children and `next` links."""
    seen: dict[str, dict[str, Any]] = {}
    todo = ["src:" + MAT_EN]
    while todo:
        card_id = todo.pop()
        if card_id in seen:
            continue
        got = await open_(w, handle, card_id, channel)
        assert isinstance(got, dict), (card_id, got)
        for card in got["cards"]:
            seen.setdefault(card["id"], card)
            todo.extend(x for x in card["next"] if x not in seen)
        todo.extend(c["id"] for c in got["cards"] if c["id"] not in seen)
    return seen


async def span_texts(w: World, handle: str) -> dict[str, str]:
    from reader.ingest import views

    async with w.engine.connect() as conn:
        pack = await views.active_pack(conn, w.users[handle], MAT_EN)
        assert pack is not None
        return {s.locator: s.text for s in await views.pack_spans(conn, pack) if s.locator is not None}


async def test_mcp_walk_on_a_personal_use_pack(w: World) -> None:
    await add_material(w, "ivan", MAT_EN, SPANS, config_id="book-en", outline=OUTLINE)
    texts = await span_texts(w, "ivan")
    cards = await walk(w, "ivan", "mcp")
    assert len(cards) == 1 + len(OUTLINE)
    quoted: dict[str, int] = {}
    cited: set[str] = set()
    for card in cards.values():
        assert card["layer"] != "L1" and not any(x.startswith("spn:") for x in card["next"])
        quotes = [c for c in card["cite"] if "quote" in c]
        assert len(quotes) <= 1
        for cite in card["cite"]:
            cited.add(cite["spn"])
            if "quote" in cite:
                assert len(cite["quote"]) <= 300 and texts[cite["spn"]].startswith(cite["quote"])
                quoted[cite["spn"]] = max(quoted.get(cite["spn"], 0), len(cite["quote"]))
    total = sum(len(t) for t in texts.values())
    assert 0 < sum(quoted.values()) <= 0.1 * total
    from reader.ingest import locator

    guesses = {locator.pdf_locator(MAT_EN, 1, 0, 5), locator.pdf_locator(MAT_EN, 2, 1, 40)}
    for spn in cited | set(texts) | guesses:
        assert await open_(w, "ivan", spn, "mcp") == "not_found"


def heading(page: int) -> str:
    return f"Heading {page} " + " ".join(f"w{page}x{k}" for k in range(8))


HEADING_ONLY = [(p, heading(p)) for p in range(1, 41)]
HEADED_BODY = [(p, t) for p in range(1, 41)
               for t in (heading(p), f"Body {p}. " + "lorem ipsum dolor " * 4)]


@pytest.mark.parametrize("spans", [HEADING_ONLY, HEADED_BODY], ids=["heading-only", "headings-and-body"])
async def test_mcp_walk_counts_titles_built_from_heading_spans(
    w: World, spans: list[tuple[int, str]]
) -> None:
    """Code review of fb7b926: an outline built from heading spans (outline_source 'headings') serves span
    text as titles; every served code point that repeats span text counts against the fraction."""
    outline = [(1, heading(p), p) for p in range(1, 41)]
    await add_material(w, "ivan", MAT_EN, spans, config_id="book-en", outline=outline)
    texts = await span_texts(w, "ivan")
    cards = await walk(w, "ivan", "mcp")
    assert len(cards) == 1 + len(outline)
    titles: set[str] = set()
    quoted: dict[str, int] = {}
    numbered = 0
    for card in cards.values():
        if card["layer"] != "L2":
            continue
        for segment in card["title"].split(" / "):
            if any(t.startswith(segment) for t in texts.values() if segment):
                titles.add(segment)
            numbered += segment.isdigit()
        for cite in card["cite"]:
            if "quote" in cite:
                quoted[cite["spn"]] = max(quoted.get(cite["spn"], 0), len(cite["quote"]))
    total = sum(len(t) for t in texts.values())
    served = sum(len(t) for t in titles) + sum(quoted.values())
    assert 0 < served <= 0.1 * total
    assert numbered > 0  # nodes past the allowance are titled by their outline number


async def test_cross_user_without_and_with_an_own_copy(w: World) -> None:
    await add_material(w, "ivan", MAT_EN, SPANS, config_id="book-en", outline=OUTLINE)
    for channel in ("mcp", "owner", "tutor", "pipeline"):
        assert await open_(w, "anna", "src:" + MAT_EN, channel) == "not_found"
        for spn in await span_texts(w, "ivan"):
            assert await open_(w, "anna", spn, channel) == "not_found"
    await add_material(w, "anna", MAT_EN, SPANS, config_id="book-en", outline=OUTLINE)
    seen = await walk(w, "anna", "tutor")
    assert any(card["layer"] == "L1" for card in seen.values())


@pytest.mark.parametrize(("channel", "license_"), [("tutor", None), ("owner", None), ("pipeline", None),
                                              ("mcp", "shareable")])
async def test_every_non_empty_span_is_reachable(w: World, channel: str, license_: str | None) -> None:
    await add_material(w, "ivan", MAT_EN, SPANS, config_id="book-en", outline=OUTLINE)
    if license_ is not None:
        await set_profile(w, "ivan", MAT_EN, license_)
    texts = await span_texts(w, "ivan")
    seen = await walk(w, "ivan", channel)
    assert {k for k, t in texts.items() if t} <= set(seen)
