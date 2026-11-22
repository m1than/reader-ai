"""Spike ingest-confine: Docling configuration D (formula model on, code model off, OCR off), CPU only,
models from a local artifacts path, no network. Runs the same way inside and outside the sandbox profile.

    <venv>/bin/python -I convert_confined.py <pdf> <pages,comma,separated> <out-dir> <artifacts-dir>

Writes per-page markdown into <out-dir> (which lives in the gitignored WORK tree) and prints one JSON line of
numbers only. It never prints document text.
"""

import json
import os
import sys
import time
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions  # noqa: E402
from docling.datamodel.base_models import InputFormat  # noqa: E402
from docling.datamodel.pipeline_options import PdfPipelineOptions  # noqa: E402
from docling.document_converter import DocumentConverter, PdfFormatOption  # noqa: E402

pdf, pages, out, artifacts = sys.argv[1], [int(p) for p in sys.argv[2].split(",")], Path(sys.argv[3]), sys.argv[4]
out.mkdir(parents=True, exist_ok=True)
opts = PdfPipelineOptions(do_ocr=False, do_table_structure=True, artifacts_path=artifacts)
opts.do_formula_enrichment = True
opts.do_code_enrichment = False
opts.accelerator_options = AcceleratorOptions(device=AcceleratorDevice.CPU, num_threads=os.cpu_count() or 1)
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
                 "formulas_decoded": md.count("$$") // 2, "not_decoded": md.count("formula-not-decoded")})
total = sum(r["seconds"] for r in rows)
print(json.dumps({"threads": os.cpu_count(), "model_load_s": round(load, 1), "seconds_total": round(total, 1),
                  "pages_per_min": round(len(rows) / (total / 60), 2), "pages": rows}))
