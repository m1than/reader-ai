"""Spike tutor-chunks: how many bytes the tutor sends when a hit carries its whole chunk's spans (SRV-TUTOR
FR-TUTOR-5/-6 revision, 2026-11-23).

Read-only and no model spend. Re-converts the pilot PDF on the host with the stack's own pinned, confined converter
(SRV-INGEST; deterministic for a converter version, so the spans equal the stack's `material_spans`), into a work
directory outside the repository, then chunks them with SRV-SEARCH's own `chunk_spans` and parameters, and measures
per chunk: the chunk's spans, each cut to span_bytes_max as the tutor cuts them, and the JSON-escaped bytes they
take in the prompt. Prints numbers only (no book text); the work directory is deleted at the end.

    uv run --project server python spikes/tutor-chunks/measure.py <pdf> <data_dir> <work_dir>
"""

from __future__ import annotations

import json
import shutil
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from reader.ingest.host import cli
from reader.search import params as search_params
from reader.search.chunking import Span, chunk_spans
from reader.tutor import params as tutor_params
from reader.tutor.answer import cut_bytes


def _q(values: list[int], p: float) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(p * len(ordered)) - 1))]


def _dist(values: list[int]) -> dict[str, Any]:
    return {
        "n": len(values),
        "mean": round(statistics.mean(values)),
        "p50": _q(values, 0.50),
        "p90": _q(values, 0.90),
        "p95": _q(values, 0.95),
        "p99": _q(values, 0.99),
        "max": max(values),
    }


def _sent_bytes(text: str) -> int:
    """Bytes one source text takes inside the prompt JSON (ensure_ascii=False, escaping included)."""
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8")) - 2


def main(argv: list[str]) -> int:
    pdf, data_dir, work = Path(argv[1]), Path(argv[2]), Path(argv[3])
    (work / "in").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pdf, work / "in" / "material.pdf")
    started = time.monotonic()
    deps = cli.real_deps(data_dir)
    try:
        members, _ = cli.convert_members(work, deps, lambda _line: None)
        convert_s = time.monotonic() - started
        rows = [json.loads(line) for line in members["spans.jsonl"].decode("utf-8").splitlines() if line.strip()]
    finally:
        shutil.rmtree(work, ignore_errors=True)
    spans = [Span(int(r["seq"]), int(r["page"]), str(r["text"])) for r in rows]
    by_seq = {s.seq: s for s in spans}
    chunks = chunk_spans(spans, search_params.CHUNK_SIZE, search_params.CHUNK_OVERLAP, search_params.CHUNK_MIN)
    limit = tutor_params.SPAN_BYTES_MAX
    per_span = [_sent_bytes(cut_bytes(s.text, limit)) for s in spans]
    primary = [_sent_bytes(cut_bytes(by_seq[c.primary_seq].text, limit)) for c in chunks]
    whole = [sum(_sent_bytes(cut_bytes(by_seq[q].text, limit)) for q in c.span_seqs) for c in chunks]
    counts = [len(c.span_seqs) for c in chunks]
    chunk_text = [len(c.text.encode("utf-8")) for c in chunks]
    report = {
        "spike": "tutor-chunks",
        "converter_s": round(convert_s, 1),
        "spans": len(spans),
        "chunks": len(chunks),
        "params": {
            "chunk_size": search_params.CHUNK_SIZE,
            "chunk_overlap": search_params.CHUNK_OVERLAP,
            "chunk_min": search_params.CHUNK_MIN,
            "span_bytes_max": limit,
        },
        "span_sent_bytes": _dist(per_span),
        "spans_per_chunk": _dist(counts),
        "chunk_text_bytes": _dist(chunk_text),
        "primary_span_sent_bytes": _dist(primary),
        "whole_chunk_spans_sent_bytes": _dist(whole),
        "spans_cut_at_span_bytes_max": sum(1 for s in spans if len(s.text.encode("utf-8")) > limit),
    }
    sys.stdout.write(json.dumps(report, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
