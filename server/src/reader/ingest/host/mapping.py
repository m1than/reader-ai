"""From converter items to spans and an outline, without Docling (SRV-INGEST FR-INGEST-5, -6, -8, -9).

Stdlib only, so the reading-order rule, the mapping and the batch loop are unit-tested with fake items.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from reader.ingest.artifact import MAX_LEVEL, MAX_PAGES, normalise

BATCH_PAGES: Final = 50  # the spike's full run used 50-page batches
Box = tuple[float, float, float, float]  # x0, y0 (top), x1, y1 in points, top-left origin

# FR-INGEST-6: Docling labels to span kinds; None drops the item (page furniture).
_KINDS: Final[Mapping[str, str | None]] = {
    "title": "heading",
    "section_header": "heading",
    "list_item": "list_item",
    "formula": "formula",
    "code": "code",
    "table": "table",
    "picture": "figure",
    "chart": "figure",
    "page_header": None,
    "page_footer": None,
}


def kind_of(label: str) -> str | None:
    return _KINDS.get(label, "text")


@dataclass
class Item:
    label: str
    text: str
    boxes: list[tuple[int, Box]]
    level: int | None = None
    decoded: bool | None = None

    @property
    def page(self) -> int:
        return self.boxes[0][0]

    @property
    def top(self) -> float:
        return self.boxes[0][1][1]


class BatchFailed(Exception):
    """A batch whose Docling status is not SUCCESS: the conversion stops without a manifest."""


def order(items: Sequence[Item]) -> list[Item]:
    """FR-INGEST-8: Docling's order, except that each code item moves to sit right after the non-code item
    on its page that comes last in Docling's order among those whose top lies above the code item's top (or
    before the page's first non-code item); code items placed at the same spot keep their order by top."""
    kept = [i for i in items if kind_of(i.label) is not None]
    pages: dict[int, list[Item]] = {}  # in the order Docling first reaches each page
    for item in kept:
        pages.setdefault(item.page, []).append(item)
    result: list[Item] = []
    for page_items in pages.values():
        plain = [i for i in page_items if kind_of(i.label) != "code"]
        codes = [i for i in page_items if kind_of(i.label) == "code"]
        if not plain:  # only code on the page: nothing to anchor to, Docling's order stays
            result.extend(codes)
            continue
        after: dict[int, list[Item]] = {}  # id(anchor) -> code items placed after it
        before: list[Item] = []  # code items placed before the page's first non-code item
        for code in codes:
            anchor = None
            for candidate in plain:
                if candidate.top < code.top:
                    anchor = candidate
            if anchor is None:
                before.append(code)
            else:
                after.setdefault(id(anchor), []).append(code)
        result.extend(sorted(before, key=lambda c: c.top))
        for item in plain:
            result.append(item)
            result.extend(sorted(after.get(id(item), []), key=lambda c: c.top))
    return result


def _bbox(box: Box) -> list[float]:
    return [round(max(0.0, v), 2) for v in box]


def spans(items: Sequence[Item]) -> list[dict[str, Any]]:
    """FR-INGEST-6: normalised spans in FR-INGEST-8's order, numbered from 0."""
    rows: list[dict[str, Any]] = []
    for item in order(items):
        kind = kind_of(item.label)
        if kind is None:
            continue
        x0, y0, x1, y1 = _bbox(item.boxes[0][1])
        level = None
        if kind == "heading":
            level = 1 if item.label == "title" else min(max(item.level or 1, 1), MAX_LEVEL)
        decoded = None
        text = normalise(item.text, code=kind == "code")
        if kind == "formula":
            decoded = bool(item.decoded) and bool(text)
            text = text if decoded else ""
        rows.append(
            {
                "seq": len(rows),
                "page": item.page,
                "kind": kind,
                "level": level,
                "decoded": decoded,
                "text": text,
                "bbox": [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)],
            }
        )
    return rows


def outline(
    bookmarks: Sequence[tuple[int, str, int | None]],
    span_rows: Sequence[Mapping[str, Any]],
    page_count: int | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """FR-INGEST-9: bookmarks (0-based level, title, 0-based page index or None), else the heading spans.

    A bookmark whose page lies outside 1..page_count gets `page: null` (the artifact validator requires it).
    """

    def page_of(index: int | None) -> int | None:
        if index is None or index < 0 or (page_count is not None and index + 1 > page_count):
            return None
        return index + 1

    if bookmarks:
        rows = [
            {
                "seq": seq,
                "level": min(level + 1, MAX_LEVEL),
                "title": normalise(title, code=False)[:1024],
                "page": page_of(page),
            }
            for seq, (level, title, page) in enumerate(bookmarks)
        ]
        return rows, "bookmarks"
    headings = [s for s in span_rows if s["kind"] == "heading"]
    rows = [
        {"seq": seq, "level": s["level"], "title": s["text"][:1024], "page": s["page"]}
        for seq, s in enumerate(headings)
    ]
    return rows, "headings"


def progress_line(done: int, total: int) -> str:
    return json.dumps({"pages_done": done, "pages_total": total}, separators=(",", ":"))


def parse_progress(line: bytes) -> tuple[int, int] | None:
    """FR-INGEST-5: a progress line is rendered only if it is exactly the schema and 0 <= n <= m <= 5,000."""
    try:
        data = json.loads(line.decode("utf-8"))
    except ValueError:
        return None
    if not isinstance(data, dict) or set(data) != {"pages_done", "pages_total"}:
        return None
    done, total = data["pages_done"], data["pages_total"]
    if type(done) is not int or type(total) is not int or not 0 <= done <= total <= MAX_PAGES:
        return None
    return done, total


def run_batches(
    convert: Callable[[int, int], tuple[str, list[Item]]],
    page_count: int,
    report: Callable[[str], None],
) -> tuple[list[Item], int]:
    """Convert pages 1..page_count in batches; `convert(first, last)` returns (status name, items)."""
    items: list[Item] = []
    converted = 0
    for first in range(1, page_count + 1, BATCH_PAGES):
        last = min(first + BATCH_PAGES - 1, page_count)
        status, batch = convert(first, last)
        if status != "SUCCESS":
            raise BatchFailed(status)
        items.extend(batch)
        converted += last - first + 1
        report(progress_line(converted, page_count))
    return items, converted
