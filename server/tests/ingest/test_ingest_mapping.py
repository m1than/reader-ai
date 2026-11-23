"""Item mapping, code indentation, reading order, outline, batches and progress, without Docling (SRV-INGEST).

@trace FR-INGEST-5 FR-INGEST-6 FR-INGEST-7 FR-INGEST-8 FR-INGEST-9
"""

from __future__ import annotations

from typing import Any

import pytest

from .conftest import need


def item(label: str, text: str, page: int, top: float, **kw: Any) -> Any:
    mapping = need("reader.ingest.host.mapping")
    return mapping.Item(label=label, text=text, boxes=[(page, (72.0, top, 500.0, top + 12.0))], **kw)


# ---- FR-INGEST-6: label mapping ----


@pytest.mark.parametrize(
    ("label", "kind"),
    [
        ("title", "heading"),
        ("section_header", "heading"),
        ("list_item", "list_item"),
        ("formula", "formula"),
        ("code", "code"),
        ("table", "table"),
        ("picture", "figure"),
        ("text", "text"),
        ("paragraph", "text"),
        ("caption", "text"),
        ("footnote", "text"),
        ("page_header", None),
        ("page_footer", None),
    ],
)
def test_label_mapping(label: str, kind: str | None) -> None:
    mapping = need("reader.ingest.host.mapping")
    assert mapping.kind_of(label) == kind


def test_spans_from_items_normalise_and_number() -> None:
    mapping = need("reader.ingest.host.mapping")
    items = [
        item("title", "Заголовок", 1, 50, level=1),
        item("text", "inter­national\x07 text", 1, 80),
        item("page_footer", "Footer 1", 1, 800),
        item("formula", "", 1, 120, decoded=False),
        item("code", "a­b", 1, 150),
    ]
    spans = mapping.spans(items)
    assert [s["kind"] for s in spans] == ["heading", "text", "formula", "code"]
    assert [s["seq"] for s in spans] == [0, 1, 2, 3]
    assert spans[0]["level"] == 1
    assert spans[1]["text"] == "international text"
    assert spans[2]["decoded"] is False and spans[2]["text"] == ""
    assert spans[3]["text"] == "a­b"
    assert spans[1]["bbox"] == [72.0, 80.0, 500.0, 92.0]


def test_page_and_bbox_come_from_the_first_box() -> None:
    mapping = need("reader.ingest.host.mapping")
    multi = mapping.Item(
        label="text", text="x", boxes=[(2, (10.0, 20.0, 30.0, 40.0)), (3, (1.0, 2.0, 3.0, 4.0))]
    )
    (span,) = mapping.spans([multi])
    assert span["page"] == 2 and span["bbox"] == [10.0, 20.0, 30.0, 40.0]


# ---- FR-INGEST-8: code-only order correction ----


def test_code_moves_after_the_last_item_above_it() -> None:
    mapping = need("reader.ingest.host.mapping")
    step1 = item("list_item", "1. step", 1, 100)
    step2 = item("list_item", "2. step", 1, 200)
    code = item("code", "$ run", 1, 150)
    after = item("text", "after", 1, 300)
    # Docling put the code block after step 2 (the pilot's pages 461/468 failure).
    ordered = mapping.order([step1, step2, after, code])
    assert [i.text for i in ordered] == ["1. step", "$ run", "2. step", "after"]


def test_code_with_nothing_above_goes_first_on_its_page() -> None:
    mapping = need("reader.ingest.host.mapping")
    prev_page = item("text", "p1", 1, 700)
    first = item("text", "below", 2, 300)
    code = item("code", "$ top", 2, 50)
    ordered = mapping.order([prev_page, first, code])
    assert [i.text for i in ordered] == ["p1", "$ top", "below"]


def test_other_items_keep_docling_order_and_code_blocks_keep_top_order() -> None:
    mapping = need("reader.ingest.host.mapping")
    a = item("text", "a", 1, 300)  # Docling order, not geometric, for non-code items
    b = item("text", "b", 1, 100)
    c1 = item("code", "c1", 1, 350)
    c2 = item("code", "c2", 1, 320)
    ordered = mapping.order([a, b, c1, c2])
    assert [i.text for i in ordered] == ["a", "b", "c2", "c1"]


# ---- FR-INGEST-9: outline ----


def test_outline_from_bookmarks() -> None:
    mapping = need("reader.ingest.host.mapping")
    outline, source = mapping.outline([(0, "Глава\x07 1", 0), (1, "Раздел", None)], [])
    assert source == "bookmarks"
    assert outline == [
        {"seq": 0, "level": 1, "title": "Глава 1", "page": 1},
        {"seq": 1, "level": 2, "title": "Раздел", "page": None},
    ]


def test_page_with_only_code_keeps_docling_order() -> None:
    mapping = need("reader.ingest.host.mapping")
    before = item("text", "p1", 1, 700)
    c_low = item("code", "c-low", 2, 400)
    c_high = item("code", "c-high", 2, 100)  # Docling put the lower block first; nothing to anchor to
    after = item("text", "p3", 3, 50)
    ordered = mapping.order([before, c_low, c_high, after])
    assert [i.text for i in ordered] == ["p1", "c-low", "c-high", "p3"]


def test_non_code_items_keep_docling_order_across_pages() -> None:
    mapping = need("reader.ingest.host.mapping")
    # Docling reaches page 2, returns to page 1 (a float), then goes on; only code may move.
    seq = [
        item("text", "a1", 1, 100),
        item("text", "b2", 2, 100),
        item("caption", "a1-float", 1, 600),
        item("code", "c1", 1, 300),
        item("text", "b2-more", 2, 400),
    ]
    ordered = [i.text for i in mapping.order(seq)]
    assert [t for t in ordered if t != "c1"] == ["a1", "b2", "a1-float", "b2-more"]
    assert ordered.index("c1") == ordered.index("a1") + 1  # after the last page-1 item above it


# ---- FR-INGEST-7: indentation from glyph origins ----

CELL = 4.05  # the pilot's monospace advance


def typeset(code: str, left: float = 96.38, top: float = 700.0) -> list[tuple[float, float]]:
    """Glyph origins of monospace `code` as a PDF places them: each non-space character at its column, no
    space glyphs; baselines 12 pt apart with a little jitter."""
    return [
        (left + col * CELL, top - row * 12.0 + (0.3 if col % 2 else 0.0))
        for row, line in enumerate(code.split("\n"))
        for col, char in enumerate(line)
        if not char.isspace()
    ]


def text_layer(code: str) -> str:
    """What pdfium's bounded text gives for such code: leading whitespace reduced to 0 or 1 space."""
    return "\n".join((" " if i % 2 else "") + line.lstrip() for i, line in enumerate(code.split("\n")))


@pytest.mark.parametrize(
    "code",
    [
        "a b c\n    d e f\n        g h",  # 0/4/8, one-letter tokens: most steps are 2 cells
        "a b c\n  d e f\n    g h",  # 0/2/4
        "a = b + c\n    d = e + f",
        "if (a < b) {\n    c = a + b;\n}",
        "pragma x;\ncontract C {\n  function f() public {\n    uint v = 200;\n  }\n}",
    ],
)
def test_code_round_trips_exactly(code: str) -> None:
    mapping = need("reader.ingest.host.mapping")
    lines = mapping.visual_lines(typeset(code))
    assert mapping.reindent(text_layer(code), lines, CELL) == (code, False)


def test_cell_is_the_font_advance_not_a_statistic_of_the_steps() -> None:
    mapping = need("reader.ingest.host.mapping")
    lines = [[0.0, 2 * CELL, 4 * CELL, 6 * CELL, 8 * CELL, 9 * CELL], [4 * CELL, 6 * CELL, 8 * CELL]]
    assert mapping.indentation(lines, CELL) == [0, 4]  # the 2-cell steps are the majority
    assert mapping.cell_of([CELL] * 9) == pytest.approx(CELL)


@pytest.mark.parametrize(
    ("advances", "monospace"),
    [([4.05, 4.05, 4.12], True), ([4.05, 4.2], False), ([4.797, 5.634], False), ([], False), ([0.0], False)],
)
def test_cell_needs_one_advance_within_two_percent(advances: list[float], monospace: bool) -> None:
    mapping = need("reader.ingest.host.mapping")
    assert (mapping.cell_of(advances) is not None) is monospace


def test_dedent_is_to_the_leftmost_line_not_the_first() -> None:
    mapping = need("reader.ingest.host.mapping")
    lines = [[4 * CELL, 5 * CELL], [0.0, CELL], [4 * CELL, 5 * CELL]]
    assert mapping.indentation(lines, CELL) == [4, 0, 4]


def test_reindent_replaces_leading_whitespace_and_never_writes_tabs() -> None:
    mapping = need("reader.ingest.host.mapping")
    code = "a = 1\n    b = 2\n        c = 3\n\nd"
    text = "a = 1\r\n b = 2\n\t\tc = 3\n\nd"
    assert mapping.reindent(text, mapping.visual_lines(typeset(code)), CELL) == (code, False)


@pytest.mark.parametrize(
    ("glyphs", "text", "cell"),
    [
        (typeset("ab\n  cd"), "ab\n cd", None),  # not monospace (no cell)
        ([(10.0, 700.0), (10.0 + CELL, 700.0), (10.0 + 1.5 * CELL, 688.0)], "ab\n c", CELL),  # half a cell
        ([(10.0, 700.0), (10.0 + CELL, 700.0), (10.0 + 2.6 * CELL, 700.0)], "abc", CELL),  # off-grid step
        (typeset("ab\n    cd"), "ab\ncd\nef", CELL),  # (c) line counts differ, the lines carry indents
    ],
)
def test_reindent_keeps_the_text_layer_when_the_rule_is_undefined(
    glyphs: list[tuple[float, float]], text: str, cell: float | None
) -> None:
    mapping = need("reader.ingest.host.mapping")
    assert mapping.reindent(text, mapping.visual_lines(glyphs), cell) == (text, True)


@pytest.mark.parametrize(("residual", "expected"), [(0.24, [0, 2]), (0.26, None)])
def test_tolerance_is_a_quarter_cell_for_offsets_and_steps(
    residual: float, expected: list[int] | None
) -> None:
    mapping = need("reader.ingest.host.mapping")
    offset = [[0.0, CELL], [(2 + residual) * CELL, (3 + residual) * CELL]]
    step = [[0.0, CELL, (3 + residual) * CELL], [2 * CELL, 3 * CELL]]
    assert mapping.indentation(offset, CELL) == expected
    assert mapping.indentation(step, CELL) == (None if expected is None else [0, 2])


def test_an_overprinted_glyph_is_a_zero_cell_step() -> None:
    mapping = need("reader.ingest.host.mapping")
    lines = [[0.0, 0.3, CELL, 2 * CELL], [4 * CELL, 4 * CELL + 0.5, 5 * CELL]]
    assert mapping.indentation(lines, CELL) == [0, 4]


@pytest.mark.parametrize(
    ("line", "rendered"),
    [
        (b'{"code_boxes":20,"indent_fallbacks":1,"multi_box_code":2}',
         "code boxes 20, indentation fallbacks 1, multi-box code items 2"),
        (b'{"code_boxes":1,"indent_fallbacks":2,"multi_box_code":0}', None),  # more fallbacks than boxes
        (b'{"code_boxes":1,"indent_fallbacks":0,"multi_box_code":0,"x":1}', None),
        (b'{"code_boxes":1,"indent_fallbacks":0,"multi_box_code":true}', None),
        (b"code boxes 1", None),
    ],
)
def test_code_line_is_rendered_only_when_valid(line: bytes, rendered: str | None) -> None:
    mapping = need("reader.ingest.host.mapping")
    assert mapping.render_code_line(line) == rendered
    if rendered is not None:
        assert mapping.code_line(20, 1, 2).encode() == line


def test_out_of_range_bookmark_page_is_null() -> None:
    mapping = need("reader.ingest.host.mapping")
    outline, _ = mapping.outline([(0, "A", 1), (0, "B", 9), (0, "C", -1)], [], 2)
    assert [o["page"] for o in outline] == [2, None, None]


def test_outline_falls_back_to_headings() -> None:
    mapping = need("reader.ingest.host.mapping")
    spans = mapping.spans([item("title", "T", 1, 10, level=1), item("text", "x", 1, 30)])
    outline, source = mapping.outline([], spans)
    assert source == "headings"
    assert outline == [{"seq": 0, "level": 1, "title": "T", "page": 1}]


# ---- FR-INGEST-5: batches and progress ----


def test_batches_of_fifty_and_progress_lines() -> None:
    mapping = need("reader.ingest.host.mapping")
    calls: list[tuple[int, int]] = []
    progress: list[str] = []

    def convert(first: int, last: int) -> tuple[str, list[Any]]:
        calls.append((first, last))
        return "SUCCESS", [item("text", f"p{first}", first, 10)]

    items, converted = mapping.run_batches(convert, 120, progress.append)
    assert calls == [(1, 50), (51, 100), (101, 120)]
    assert converted == 120 and len(items) == 3
    assert progress == [
        '{"pages_done":50,"pages_total":120}',
        '{"pages_done":100,"pages_total":120}',
        '{"pages_done":120,"pages_total":120}',
    ]


@pytest.mark.parametrize("status", ["PARTIAL_SUCCESS", "FAILURE", "SKIPPED", "PENDING", "STARTED"])
def test_non_success_batch_stops_the_conversion(status: str) -> None:
    mapping = need("reader.ingest.host.mapping")

    def convert(first: int, last: int) -> tuple[str, list[Any]]:
        return ("SUCCESS" if first == 1 else status), []

    with pytest.raises(mapping.BatchFailed):
        mapping.run_batches(convert, 120, lambda _: None)


@pytest.mark.parametrize(
    ("line", "parsed"),
    [
        (b'{"pages_done":5,"pages_total":10}', (5, 10)),
        (b'{"pages_done": 0, "pages_total": 0}', (0, 0)),
        (b'{"pages_done":11,"pages_total":10}', None),
        (b'{"pages_done":5,"pages_total":5001}', None),
        (b'{"pages_done":5}', None),
        (b'{"pages_done":5,"pages_total":10,"x":1}', None),
        (b'{"pages_done":true,"pages_total":10}', None),
        (b"QUOKKAZEBRA", None),
        (b"\xff\xfe", None),
    ],
)
def test_progress_lines_are_rendered_only_when_valid(line: bytes, parsed: tuple[int, int] | None) -> None:
    mapping = need("reader.ingest.host.mapping")
    assert mapping.parse_progress(line) == parsed
