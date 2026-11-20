# /// script
# requires-python = ">=3.14,<3.15"
# dependencies = ["docling[easyocr]==2.151.1"]
# ///
"""Spike ingest-docling, native path (ADR-0008 fallback): Docling on the macOS host instead of Docker.
Layout and tables pick their device automatically (MPS when available); the formula/code model runs on the
host CPU with all cores (Docling refuses MPS for it). Converted text goes to the gitignored data/ dir.

    uv run --script spikes/ingest-docling/convert_native.py <pdf> <pages,comma,separated> <out-dir>
"""

import json
import os
import sys
import time
from pathlib import Path

import torch
from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption

pdf, pages, out = sys.argv[1], [int(p) for p in sys.argv[2].split(",")], Path(sys.argv[3])
out.mkdir(parents=True, exist_ok=True)
opts = PdfPipelineOptions(do_ocr=False, do_table_structure=True)
opts.do_formula_enrichment = True
opts.do_code_enrichment = os.environ.get("CODE", "1") == "1"   # CODE=0: formulas only (code OCR corrupts text)
opts.accelerator_options = AcceleratorOptions(device=AcceleratorDevice.AUTO, num_threads=os.cpu_count())
conv = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
t0 = time.monotonic()
conv.initialize_pipeline(InputFormat.PDF)
load = time.monotonic() - t0
rows = []
for p in pages:
    t1 = time.monotonic()
    doc = conv.convert(pdf, page_range=(p, p)).document
    md = doc.export_to_markdown()
    (out / f"p{p:04d}.md").write_text(md)
    rows.append({"page": p, "seconds": round(time.monotonic() - t1, 1), "tables": len(doc.tables),
                 "formulas_decoded": md.count("$$") // 2, "not_decoded": md.count("formula-not-decoded"),
                 "code_blocks": md.count("```") // 2})
    print(json.dumps({"progress": rows[-1]}), flush=True)
print(json.dumps({"mode": "sample-native", "host": "macOS native", "threads": os.cpu_count(),
                  "mps_available": torch.backends.mps.is_available(), "model_load_s": round(load, 1),
                  "seconds_total": round(sum(r["seconds"] for r in rows), 1), "pages": rows}))
