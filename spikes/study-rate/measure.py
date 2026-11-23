"""Spike study-rate, step 1 (no model spend, numbers only): characters per PDF page of the pilot.

Derives the planner's default study rate (minutes per 1,000 characters) from the owner's own hand-made study plan
(pilot/bashir/plan.yaml: theory hours per stage) and the text layer of the pages those stages cover
(pilot/bashir/course.yaml: PDF page ranges per lesson). Also records L02's characters for the compile-cost scaling.

Usage:
  uv run --project server python -I spikes/study-rate/measure.py <pilot.pdf> <out.json>
      characters from the PDF text layer (pypdfium2);
  uv run --project server python -I spikes/study-rate/measure.py <pilot.pdf> <out.json> <data_dir> <work_dir>
      characters of the stack's own spans, re-converted with the pinned confined converter (what the planner counts).
The PDF is the owner's copyrighted file; only counts leave this script.
"""

from __future__ import annotations

import hashlib
import json
import statistics
import sys
from pathlib import Path

import pypdfium2 as pdfium
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
PILOT_SHA256 = "61e8d4c390ba63c3f13efe0860d5042f8d1b19ba8f26b4f2e14e73478bc9e2b2"


def page_chars(pdf: pdfium.PdfDocument) -> list[int]:
    out: list[int] = []
    for i in range(len(pdf)):
        page = pdf[i]
        tp = page.get_textpage()
        text = tp.get_text_range().replace("\r\n", "\n")
        out.append(len(text))
        tp.close()
        page.close()
    return out


def span_page_chars(pdf_path: str, data_dir: str, work_dir: str, pages: int) -> list[int]:
    """Characters per page as the planner counts them: the sum of len(span text) of the stack's own spans.

    Re-converts the PDF with the stack's pinned, confined converter (SRV-INGEST; deterministic for a converter
    version, as in spikes/tutor-chunks), into a work directory outside the repository that is deleted afterwards.
    """
    import shutil

    from reader.ingest.host import cli

    work = Path(work_dir)
    (work / "in").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pdf_path, work / "in" / "material.pdf")
    try:
        members, _ = cli.convert_members(work, cli.real_deps(Path(data_dir)), lambda _line: None)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    out = [0] * pages
    for line in members["spans.jsonl"].decode("utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            out[int(row["page"]) - 1] += len(str(row["text"]))
    return out


def main(pdf_path: str, out_path: str, data_dir: str | None = None, work_dir: str | None = None) -> None:
    data = Path(pdf_path).read_bytes()
    if hashlib.sha256(data).hexdigest() != PILOT_SHA256:
        sys.exit("not the pilot PDF (sha256 mismatch)")
    yaml = YAML(typ="safe")
    plan = yaml.load((ROOT / "pilot/bashir/plan.yaml").read_text(encoding="utf-8"))
    course = yaml.load((ROOT / "pilot/bashir/course.yaml").read_text(encoding="utf-8"))
    chars = page_chars(pdfium.PdfDocument(data))
    basis = "text_layer"
    if data_dir and work_dir:
        chars = span_page_chars(pdf_path, data_dir, work_dir, len(chars))
        basis = "spans"

    theory_h = {s["id"]: float(s.get("theory_h", 0)) for s in plan["stages"]}
    per_stage = []
    lesson_pages: dict[str, tuple[int, int]] = {}
    for st in course["stages"]:
        pages = 0
        kchars = 0.0
        for lesson in st["lessons"]:
            a, b = lesson["pages"]["pdf"]
            lesson_pages[lesson["id"]] = (a, b)
            pages += b - a + 1
            kchars += sum(chars[a - 1 : b]) / 1000
        per_stage.append(
            {
                "stage": st["id"],
                "pages": pages,
                "kchars": round(kchars, 3),
                "theory_min": theory_h[st["id"]] * 60,
                "min_per_kchar": round(theory_h[st["id"]] * 60 / kchars, 4) if kchars else None,
            }
        )
    total_pages = sum(s["pages"] for s in per_stage)
    total_kchars = sum(s["kchars"] for s in per_stage)
    total_min = sum(s["theory_min"] for s in per_stage)
    rates = [s["min_per_kchar"] for s in per_stage if s["min_per_kchar"] is not None and s["theory_min"] > 0]
    a, b = lesson_pages["L02"]
    planned = [chars[p - 1] for lp in lesson_pages.values() for p in range(lp[0], lp[1] + 1)]
    result = {
        "basis": basis,
        "pdf_pages": len(chars),
        "planned_pages": total_pages,
        "planned_kchars": round(total_kchars, 3),
        "theory_min": total_min,
        "min_per_page": round(total_min / total_pages, 4),
        "min_per_kchar": round(total_min / total_kchars, 4),
        "min_per_kchar_by_stage": {"min": min(rates), "median": statistics.median(rates), "max": max(rates)},
        "chars_per_page": {
            "mean": round(statistics.mean(planned), 1),
            "p10": statistics.quantiles(planned, n=10)[0],
            "median": statistics.median(planned),
            "p90": statistics.quantiles(planned, n=10)[-1],
        },
        "l02": {"pdf_pages": [a, b], "kchars": round(sum(chars[a - 1 : b]) / 1000, 3)},
        "per_stage": per_stage,
    }
    Path(out_path).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("planned_pages", "planned_kchars", "min_per_kchar", "l02")}))


if __name__ == "__main__":
    main(*sys.argv[1:5])
