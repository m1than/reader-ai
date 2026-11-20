"""Spike ingest-docling (ADR-0008). Runs inside the spike image; writes to /out (a gitignored host dir).

    convert.py full   <pdf> [--batch 50]          all pages in batches: timings and markdown per batch
    convert.py sample <pdf> --pages 12,57,...     given pages with formula enrichment: markdown per page
    convert.py ocr    <pdf> --pages 12,57,...     pages rendered as images, full-page OCR (ru, en),
                                                  character accuracy against the PDF's text layer

Converted text is copyrighted material: it stays in /out and never enters the repository.
Each mode prints one JSON object with the measurements.
"""

import argparse
import json
import time
from pathlib import Path

import pypdfium2 as pdfium
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import EasyOcrOptions, PdfPipelineOptions
from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption
from rapidfuzz.distance import Levenshtein

OUT = Path("/out")
MODELS = "/models"


def converter(*, formulas: bool = False, full_page_ocr: bool = False) -> DocumentConverter:
    opts = PdfPipelineOptions(artifacts_path=MODELS)
    opts.do_ocr = True  # bitmap regions only, unless full_page_ocr
    opts.ocr_options = EasyOcrOptions(lang=["ru", "en"], force_full_page_ocr=full_page_ocr)
    opts.do_table_structure = True
    opts.do_formula_enrichment = formulas
    return DocumentConverter(format_options={
        InputFormat.PDF: PdfFormatOption(pipeline_options=opts),
        InputFormat.IMAGE: ImageFormatOption(pipeline_options=opts),
    })


def full(pdf: str, batch: int) -> dict[str, object]:
    pages = len(pdfium.PdfDocument(pdf))
    conv = converter()
    out = OUT / "full"
    out.mkdir(parents=True, exist_ok=True)
    batches = []
    t0 = time.monotonic()
    for start in range(1, pages + 1, batch):
        end = min(start + batch - 1, pages)
        t1 = time.monotonic()
        doc = conv.convert(pdf, page_range=(start, end)).document
        (out / f"p{start:04d}-{end:04d}.md").write_text(doc.export_to_markdown())
        seconds = time.monotonic() - t1
        batches.append({"pages": [start, end], "seconds": round(seconds, 1),
                        "tables": len(doc.tables), "pictures": len(doc.pictures)})
        print(json.dumps({"progress": batches[-1]}), flush=True)
    total = time.monotonic() - t0
    return {"mode": "full", "pages": pages, "seconds": round(total, 1),
            "pages_per_min": round(pages / total * 60, 2), "batches": batches}


def sample(pdf: str, pages: list[int]) -> dict[str, object]:
    conv = converter(formulas=True)
    out = OUT / "sample"
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for p in pages:
        t1 = time.monotonic()
        doc = conv.convert(pdf, page_range=(p, p)).document
        (out / f"p{p:04d}.md").write_text(doc.export_to_markdown())
        rows.append({"page": p, "seconds": round(time.monotonic() - t1, 1), "tables": len(doc.tables),
                     "formulas": sum(1 for t in doc.texts if t.label == "formula")})
    return {"mode": "sample", "pages": rows}


def norm(s: str) -> str:
    return " ".join(s.split())


def ocr(pdf: str, pages: list[int]) -> dict[str, object]:
    src = pdfium.PdfDocument(pdf)
    conv = converter(full_page_ocr=True)
    out = OUT / "ocr"
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for p in pages:
        page = src[p - 1]
        image = out / f"p{p:04d}.png"
        page.render(scale=200 / 72).to_pil().save(image)  # 200 dpi, no text layer
        truth = norm(page.get_textpage().get_text_range())
        t1 = time.monotonic()
        doc = conv.convert(str(image)).document
        text = norm(doc.export_to_text())
        (out / f"p{p:04d}.txt").write_text(text)
        rows.append({"page": p, "seconds": round(time.monotonic() - t1, 1), "chars": len(truth),
                     "char_accuracy": round(1 - Levenshtein.normalized_distance(truth, text), 4)})
    accs = [r["char_accuracy"] for r in rows]
    return {"mode": "ocr", "dpi": 200, "lang": ["ru", "en"], "pages": rows,
            "char_accuracy_mean": round(sum(accs) / len(accs), 4), "char_accuracy_min": min(accs)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["full", "sample", "ocr"])
    ap.add_argument("pdf")
    ap.add_argument("--batch", type=int, default=50)
    ap.add_argument("--pages", default="")
    a = ap.parse_args()
    pages = [int(x) for x in a.pages.split(",") if x]
    result = full(a.pdf, a.batch) if a.mode == "full" else sample(a.pdf, pages) if a.mode == "sample" \
        else ocr(a.pdf, pages)
    print(json.dumps(result))


main()
