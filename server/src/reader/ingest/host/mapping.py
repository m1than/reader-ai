"""From converter items to spans and an outline, without Docling (SRV-INGEST FR-INGEST-5..9).

Stdlib only, so the reading-order rule, the mapping and the batch loop are unit-tested with fake items.
"""

from __future__ import annotations

import itertools
import json
from collections import Counter
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


BASELINE_PT: Final = 1.0  # FR-INGEST-7: glyphs within 1 pt of a line's first baseline share the line
INDENT_TOLERANCE: Final = 0.25  # FR-INGEST-7, a chosen bound: |q - n| allowed, in cells (pilot: 0.00)
MONO_SPREAD: Final = 0.02  # FR-INGEST-7, a chosen bound: a glyph within 2% of the modal advance is in band
MONO_SHARE: Final = 0.8  # FR-INGEST-7, a chosen bound: at least 80% of a box's glyphs in band
Glyph = tuple[float, float, float]  # origin x, baseline y (bottom-left origin), advance; points


@dataclass(frozen=True)
class Grid:
    """FR-INGEST-7: a box's monospace cell and the modal advance that defines the band."""

    cell: float
    mode: float
    complete: bool  # every glyph in band (False: recovered by the majority cell)

    def in_band(self, advance: float) -> bool:
        return abs(advance - self.mode) <= MONO_SPREAD * self.mode


def visual_lines(glyphs: Sequence[Glyph]) -> list[list[tuple[float, float]]]:
    """FR-INGEST-7: glyphs grouped into lines by baseline, top line first; each line is its glyphs'
    (origin x, advance), sorted by x."""
    lines: list[tuple[float, list[tuple[float, float]]]] = []
    for x, y, advance in sorted(glyphs, key=lambda g: -g[1]):
        if lines and abs(lines[-1][0] - y) <= BASELINE_PT:
            lines[-1][1].append((x, advance))
        else:
            lines.append((y, [(x, advance)]))
    return [sorted(line) for _, line in lines]


def grid_of(advances: Sequence[float]) -> Grid | None:
    """FR-INGEST-7: the modal advance (0.01 pt; the smaller on a tie) and the mean in-band advance as the
    cell, or None if fewer than MONO_SHARE of the glyphs are in band."""
    if not advances or min(advances) <= 0:
        return None
    counts = Counter(round(a, 2) for a in advances)
    mode = min(counts, key=lambda width: (-counts[width], width))
    band = [a for a in advances if abs(a - mode) <= MONO_SPREAD * mode]
    if len(band) < MONO_SHARE * len(advances):
        return None
    return Grid(cell=sum(band) / len(band), mode=mode, complete=len(band) == len(advances))


def indentation(lines: Sequence[Sequence[tuple[float, float]]], grid: Grid | None) -> list[int] | None:
    """FR-INGEST-7: leading spaces per visual line, or None when the rule is not well defined: each step
    between two consecutive in-band glyphs, and every line's offset, must be a whole number of cells.
    The cell comes from the font's advance, never from the steps (one-letter tokens make every step 2 cells).
    """
    if grid is None or not lines:
        return None

    def cells(distance: float) -> int | None:
        q = distance / grid.cell
        n = round(q)
        return n if abs(q - n) <= INDENT_TOLERANCE else None

    for line in lines:
        for (xa, aa), (xb, ab) in itertools.pairwise(line):
            if grid.in_band(aa) and grid.in_band(ab) and cells(xb - xa) is None:
                return None  # not on one monospace grid
    left = min(line[0][0] for line in lines)  # dedent to the leftmost line
    counts = [cells(line[0][0] - left) for line in lines]
    return None if None in counts else [n for n in counts if n is not None]


def reindent(text: str, glyphs: Sequence[Glyph]) -> tuple[str, str]:
    """FR-INGEST-7: replace each non-empty text line's leading whitespace by its measured indentation.
    Returns the text and how: `exact`, `majority` (out-of-band glyphs skipped) or `fallback` (the text
    layer's own indentation: rule undefined or line counts differ)."""
    rows = text.replace("\r\n", "\n").split("\n")
    filled = [i for i, row in enumerate(rows) if row.strip()]
    grid = grid_of([g[2] for g in glyphs])
    counts = indentation(visual_lines(glyphs), grid)
    if grid is None or counts is None or len(counts) != len(filled):
        return text, "fallback"
    for i, n in zip(filled, counts, strict=True):
        rows[i] = " " * n + rows[i].lstrip()
    return "\n".join(rows), "exact" if grid.complete else "majority"


class BatchFailed(Exception):
    """A batch whose Docling status is not SUCCESS: the conversion stops without a manifest."""


def order(items: Sequence[Item]) -> list[Item]:
    """FR-INGEST-8: Docling's order, except that each code item moves to sit right after the non-code item
    on its page that comes last in Docling's order among those whose top lies above the code item's top (or
    before the page's first non-code item); code items placed at the same spot keep their order by top."""
    kept = [i for i in items if kind_of(i.label) is not None]
    plain = [i for i in kept if kind_of(i.label) != "code"]
    plain_pages = {i.page for i in plain}
    after: dict[int, list[Item]] = {}  # id(anchor) -> code items placed after it
    before: dict[int, list[Item]] = {}  # page -> code items placed before its first non-code item
    for code in (i for i in kept if kind_of(i.label) == "code" and i.page in plain_pages):
        anchor = None
        for candidate in plain:
            if candidate.page == code.page and candidate.top < code.top:
                anchor = candidate
        if anchor is None:
            before.setdefault(code.page, []).append(code)
        else:
            after.setdefault(id(anchor), []).append(code)
    result: list[Item] = []
    started: set[int] = set()
    for item in kept:  # non-code items in exactly Docling's order; code only where FR-INGEST-8 puts it
        if kind_of(item.label) == "code":
            if item.page not in plain_pages:  # a page of only code: Docling's order and position
                result.append(item)
            continue
        if item.page not in started:
            started.add(item.page)
            result.extend(sorted(before.get(item.page, []), key=lambda c: c.top))
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


CODE_KEYS: Final = ("code_boxes", "indent_fallbacks", "majority_cell", "multi_box_code")
MAX_CODE_BOXES: Final = 1_000_000


def code_line(boxes: int, fallbacks: int, majority: int, multi_box: int) -> str:
    """FR-INGEST-5: the converter's one code line after the last batch, numbers only."""
    values = (boxes, fallbacks, majority, multi_box)
    return json.dumps(dict(zip(CODE_KEYS, values, strict=True)), separators=(",", ":"))


def render_code_line(line: bytes) -> str | None:
    """FR-INGEST-5: the code line rendered for the owner, only if it is exactly its schema and bounds."""
    try:
        data = json.loads(line.decode("utf-8"))
    except ValueError:
        return None
    if not isinstance(data, dict) or set(data) != set(CODE_KEYS):
        return None
    boxes, fallbacks, majority, multi = (data[k] for k in CODE_KEYS)
    if any(type(v) is not int for v in (boxes, fallbacks, majority, multi)):
        return None
    if not (0 <= fallbacks <= boxes <= MAX_CODE_BOXES and 0 <= majority <= boxes and 0 <= multi <= boxes):
        return None
    return (
        f"code boxes {boxes}, indentation fallbacks {fallbacks}, majority-cell boxes {majority},"
        f" multi-box code items {multi}"
    )


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
