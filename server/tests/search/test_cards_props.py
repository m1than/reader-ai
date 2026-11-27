"""SRV-SEARCH [unit] properties of the per-material quote allowance over generated outlines.

@trace FR-SEARCH-30

Outlines are generated with deep trees, many short sections, sections that share a first span, pages without a
section and spans shorter and longer than 300 code points. The allowance is computed by the pure `Book`.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from .conftest import need

SHA = "c" * 64
WORDS = st.text(alphabet="abcdefg ", min_size=0, max_size=700)


@st.composite
def books(draw: Any) -> tuple[list[tuple[int, str]], list[tuple[int, str, int | None]]]:
    pages = draw(st.integers(min_value=1, max_value=6))
    spans: list[tuple[int, str]] = []
    for page in range(1, pages + 1):
        for body in draw(st.lists(WORDS, min_size=0, max_size=5)):
            spans.append((page, body))
    count = draw(st.integers(min_value=0, max_value=40))
    levels: list[int] = []
    for _ in range(count):
        top = (levels[-1] + 1) if levels else 1
        levels.append(draw(st.integers(min_value=1, max_value=min(top, 16))))
    outline = [(level, f"S{i}", draw(st.one_of(st.none(), st.integers(min_value=1, max_value=pages))))
               for i, level in enumerate(levels)]
    return spans, outline


def build(spans: list[tuple[int, str]], outline: list[tuple[int, str, int | None]]) -> Any:
    opening = need("reader.search.opening")
    policy = need("reader.search.policy").policy()
    from reader.ingest import locator

    rows = []
    for page in sorted({p for p, _ in spans}):
        texts = [t for p, t in spans if p == page]
        for body, loc in zip(texts, locator.page_locators(SHA, page, texts), strict=True):
            rows.append(opening.Span(loc, page, body, locator.quote_sha256(body)))
    paths = locator.outline_paths([level for level, _, _ in outline])
    nodes = [opening.Node(locator.outline_id(SHA, p), level, title, page)
             for (level, title, page), p in zip(outline, paths, strict=True)]
    return opening.Book(SHA, "p" * 64, rows, nodes, {"license": "personal-use", "language": "en"}, policy)


@settings(max_examples=150, deadline=None)
@given(books())
def test_allowance_bounds_prefix_and_served_quotes(case: Any) -> None:
    spans, outline = case
    book = build(spans, outline)
    cards = need("reader.search.cards")
    policy = book.policy
    fraction = policy.data["walk_quote_fraction_max"]
    limit = policy.data["quote_max_chars"]
    order = [n.id for n in book.nodes]
    granted = book.granted
    # a prefix of outline order, with no gap
    assert granted == frozenset(order[: len(granted)])
    # distinct quoted code points (full quotes) within the fraction
    counted: dict[str, int] = {}
    for i, node in enumerate(book.nodes):
        cited = book.cited[i]
        if node.id in granted and cited is not None:
            span = book.spans[cited]
            counted[span.locator] = len(cards.quote(span.text, limit))
    assert sum(counted.values()) <= fraction * book.total
    for channel in ("mcp", "owner", "tutor", "pipeline"):
        for i, node in enumerate(book.nodes):
            card = book.node_card(i, channel, True)
            quoted = [c for c in card.cite if c.quote is not None]
            assert len(quoted) <= policy.data["quotes_per_card_max"]
            for position, cite in enumerate(card.cite):
                if cite.quote is None:
                    continue
                assert position == 0 and node.id in granted
                full = cards.quote(book.spans[book.span_index[cite.spn]].text, limit)
                assert full.startswith(cite.quote) and len(cite.quote) <= limit
    # deterministic: the same inputs give the same allowance
    assert build(spans, outline).granted == granted


def test_many_short_sections_stop_at_the_fraction() -> None:
    spans = [(1, "word " * 4)] +[(p, f"s{p} " + "x" * 40) for p in range(2, 40)]
    outline = [(1, f"T{p}", p) for p in range(1, 40)]
    book = build(spans, outline)
    cards = need("reader.search.cards")
    counted = sum(len(cards.quote(book.spans[book.cited[i]].text, 300))
                  for i, n in enumerate(book.nodes) if n.id in book.granted and book.cited[i] is not None)
    assert counted <= 0.1 * book.total
    assert 0 < len(book.granted) < len(book.nodes)
