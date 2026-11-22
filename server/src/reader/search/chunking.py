"""Chunks from material spans (SRV-SEARCH FR-SEARCH-3, VEC-SEARCH-2).

Per page, in span seq order: each span's text is whitespace-collapsed; empty results are skipped; the rest are
joined with one space. Windows start at range(0, max(len - overlap, 1), size - overlap) and take
text[start:start + size]; a window shorter than `min_len` is dropped. The chunk lists every span whose
character range intersects the window; its primary span has the largest overlap, the first on a tie.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import groupby


@dataclass(frozen=True)
class Span:
    seq: int
    page: int
    text: str


@dataclass(frozen=True)
class Chunk:
    page: int
    chunk_no: int
    char_start: int
    char_stop: int
    text: str
    span_seqs: tuple[int, ...]
    primary_seq: int


def chunk_spans(spans: Sequence[Span], size: int, overlap: int, min_len: int) -> list[Chunk]:
    ordered = sorted(spans, key=lambda s: (s.page, s.seq))
    chunks: list[Chunk] = []
    for page, group in groupby(ordered, key=lambda s: s.page):
        text = ""
        ranges: list[tuple[int, int, int]] = []
        for span in group:
            body = " ".join(span.text.split())
            if not body:
                continue
            if text:
                text += " "
            ranges.append((span.seq, len(text), len(text) + len(body)))
            text += body
        for start in range(0, max(len(text) - overlap, 1), size - overlap):
            piece = text[start : start + size]
            if len(piece) < min_len:
                continue
            stop = start + len(piece)
            overlaps = [(seq, min(b, stop) - max(a, start)) for seq, a, b in ranges if a < stop and b > start]
            best = max(o for _, o in overlaps)
            primary = next(seq for seq, o in overlaps if o == best)
            chunks.append(
                Chunk(page, len(chunks), start, stop, piece, tuple(seq for seq, _ in overlaps), primary)
            )
    return chunks
