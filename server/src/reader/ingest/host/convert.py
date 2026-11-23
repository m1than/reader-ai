"""The confined converter: Docling configuration D on CPU, in batches, into an artifact (FR-INGEST-5..10).

    <converter venv>/bin/python -I -m reader.ingest.host.convert --input <pdf> --models <dir> --out <dir>

Runs only inside the sandbox profile (FR-INGEST-4), from the converter venv (FR-INGEST-3). Stdout carries only
progress lines; nothing it prints holds document text. Exit 0 with an artifact, 3 when a batch is not SUCCESS
(no manifest written), 2 on bad arguments.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path
from typing import Any, Final

from reader.ingest.artifact import write_artifact
from reader.ingest.host.mapping import (
    BatchFailed,
    Item,
    outline,
    reindent,
    run_batches,
    spans,
    visual_lines,
)

TEXTLESS_BELOW: Final = 200  # characters in a page's text layer (the ingest-docling spike's threshold)
# Points added around a code item's box before reading the text layer. Structural: Docling's boxes hug the
# glyph ink, and 1 pt (well under a line's 2 pt leading gap in the pilot) keeps edge glyphs without reaching
# the next line. Checked by VEC-INGEST-3: the pilot's code hashes, from wider hand-recorded boxes, match.
CODE_MARGIN_PT: Final = 1.0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bookmarks(pdf: Any) -> list[tuple[int, str, int | None]]:
    """The bookmark tree as (0-based level, title, 0-based page index or None), pypdfium2 5.x API."""
    marks: list[tuple[int, str, int | None]] = []
    for mark in pdf.get_toc():
        dest = mark.get_dest()
        index = dest.get_index() if dest is not None else None
        marks.append((int(mark.level), str(mark.get_title() or ""), None if index is None else int(index)))
    return marks


def read_outline(path: str | Path) -> list[dict[str, Any]]:
    """FR-INGEST-9: the bookmark outline of `path` (empty without bookmarks)."""
    import pypdfium2

    pdf = pypdfium2.PdfDocument(str(path))
    try:
        marks, page_count = bookmarks(pdf), len(pdf)
    finally:
        pdf.close()
    return outline(marks, [], page_count)[0] if marks else []


def _glyph_origins(text_page: Any, box: tuple[float, float, float, float]) -> list[tuple[float, float]]:
    """FR-INGEST-7: the origins (pen x, baseline y) of the non-whitespace characters whose glyph-box centre
    lies inside `box` (left, bottom, right, top; bottom-left origin)."""
    import ctypes

    import pypdfium2.raw as pdfium_c

    left, bottom, right, top = box
    origins: list[tuple[float, float]] = []
    x, y = ctypes.c_double(), ctypes.c_double()
    for index in range(text_page.count_chars()):
        char = text_page.get_text_range(index, 1)
        if not char or char.isspace():
            continue
        g_left, g_bottom, g_right, g_top = text_page.get_charbox(index)
        if not (left <= (g_left + g_right) / 2 <= right and bottom <= (g_bottom + g_top) / 2 <= top):
            continue
        if pdfium_c.FPDFText_GetCharOrigin(text_page, index, x, y):
            origins.append((float(x.value), float(y.value)))
    return origins


def _code_text(pdf: Any, prov: list[Any], heights: dict[int, float]) -> str:
    """FR-INGEST-7: the text layer inside every provenance box, reindented, in order, joined with newlines."""
    parts: list[str] = []
    for p in prov:
        box = p.bbox.to_bottom_left_origin(heights[p.page_no])
        page = pdf[p.page_no - 1]
        text_page = page.get_textpage()
        left, right = box.l - CODE_MARGIN_PT, box.r + CODE_MARGIN_PT
        bottom, top = min(box.b, box.t) - CODE_MARGIN_PT, max(box.b, box.t) + CODE_MARGIN_PT
        try:
            text = text_page.get_text_bounded(left=left, bottom=bottom, right=right, top=top)
            glyphs = _glyph_origins(text_page, (left, bottom, right, top))
            parts.append(reindent(text, visual_lines(glyphs)))
        finally:
            text_page.close()
            page.close()
    return "\n".join(parts)


def _items(doc: Any, pdf: Any) -> list[Item]:
    from docling_core.types.doc.document import (
        FormulaItem,
        ListItem,
        PictureItem,
        SectionHeaderItem,
        TableItem,
        TextItem,
    )

    heights = {no: page.size.height for no, page in doc.pages.items()}
    items: list[Item] = []
    for node, _ in doc.iterate_items():
        prov = list(getattr(node, "prov", []) or [])
        if not prov:
            continue
        label = str(node.label.value)
        boxes = []
        for p in prov:
            b = p.bbox.to_top_left_origin(heights[p.page_no])
            boxes.append((int(p.page_no), (float(b.l), float(b.t), float(b.r), float(b.b))))
        level = None
        decoded = None
        if label == "code":
            text = _code_text(pdf, prov, heights)
        elif isinstance(node, TableItem):
            text = node.export_to_markdown(doc=doc)
        elif isinstance(node, PictureItem):
            text = node.caption_text(doc)
        elif isinstance(node, FormulaItem):
            text = node.text or ""
            decoded = bool(text.strip())
        elif isinstance(node, TextItem):
            text = node.text or ""
            if isinstance(node, SectionHeaderItem):
                level = int(node.level)
            enumerated = isinstance(node, ListItem) and bool(getattr(node, "enumerated", False))
            marker = str(getattr(node, "marker", "") or "").strip() if enumerated else ""
            if marker and not text.startswith(marker):
                text = f"{marker} {text}"  # numbered steps keep their numbers (Docling stores them apart)
        else:
            text = ""
        items.append(Item(label=label, text=text, boxes=boxes, level=level, decoded=decoded))
    return items


def convert(source: Path, models: Path, out: Path, report: Any = None) -> int:
    import pypdfium2
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    def emit(line: str) -> None:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    report = report or emit
    options = PdfPipelineOptions(do_ocr=False, do_table_structure=True, artifacts_path=str(models))
    options.do_formula_enrichment = True
    options.do_code_enrichment = False
    options.accelerator_options = AcceleratorOptions(
        device=AcceleratorDevice.CPU, num_threads=os.cpu_count() or 1
    )
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
    converter.initialize_pipeline(InputFormat.PDF)

    pdf = pypdfium2.PdfDocument(str(source))
    try:
        page_count = len(pdf)
        without: list[int] = []
        for index in range(page_count):
            page = pdf[index]
            text_page = page.get_textpage()
            if text_page.count_chars() < TEXTLESS_BELOW:
                without.append(index + 1)
            text_page.close()
            page.close()
        marks = bookmarks(pdf)

        def batch(first: int, last: int) -> tuple[str, list[Item]]:
            result = converter.convert(source, page_range=(first, last), raises_on_error=False)
            name = str(result.status.name)
            return name, (_items(result.document, pdf) if name == "SUCCESS" else [])

        try:
            items, converted = run_batches(batch, page_count, report)
        except BatchFailed:
            return 3
        rows = spans(items)
        entries, source_kind = outline(marks, rows, page_count)
        write_artifact(
            out,
            material_sha256=_sha256(source),
            page_count=page_count,
            pages_converted=converted,
            pages_without_text=without,
            outline_source=source_kind,
            spans=rows,
            outline=entries,
        )
        return 0
    finally:
        pdf.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reader.ingest.host.convert")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--models", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    return convert(args.input, args.models, args.out)


if __name__ == "__main__":
    sys.exit(main())
