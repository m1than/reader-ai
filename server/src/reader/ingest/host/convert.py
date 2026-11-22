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
from reader.ingest.host.mapping import BatchFailed, Item, outline, run_batches, spans

TEXTLESS_BELOW: Final = 200  # characters in a page's text layer (the ingest-docling spike's threshold)
CODE_MARGIN_PT: Final = 1.0  # the text-layer box around a code item, so edge glyphs are not cut


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
        marks = bookmarks(pdf)
    finally:
        pdf.close()
    return outline(marks, [])[0] if marks else []


def _code_text(pdf: Any, prov: list[Any], heights: dict[int, float]) -> str:
    """FR-INGEST-7: the text layer inside every provenance box, in order, joined with newlines."""
    parts: list[str] = []
    for p in prov:
        box = p.bbox.to_bottom_left_origin(heights[p.page_no])
        page = pdf[p.page_no - 1]
        text_page = page.get_textpage()
        try:
            parts.append(
                text_page.get_text_bounded(
                    left=box.l - CODE_MARGIN_PT,
                    bottom=min(box.b, box.t) - CODE_MARGIN_PT,
                    right=box.r + CODE_MARGIN_PT,
                    top=max(box.b, box.t) + CODE_MARGIN_PT,
                )
            )
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
            marker = str(getattr(node, "marker", "") or "").strip() if isinstance(node, ListItem) else ""
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
        entries, source_kind = outline(marks, rows)
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
