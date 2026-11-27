"""Span windows (SRV-SEARCH FR-SEARCH-3, VEC-SEARCH-2).

The unit of retrieval is one span. Its text alone is cut into windows that start at
range(0, max(len - overlap, 1), width - overlap) and take text[start:start + width]; the first window has no
minimum (a short span is one window) and a later window shorter than `min_len` is merged into the previous one
(the previous window's stop moves to the later one's). A later window is at least overlap + 1 characters long,
so with the production parameters (overlap 200, minimum 80) nothing merges and every passage stays within
`size`. A prefix shrinks the width to size - len(prefix) - 1 and the passage is prefix + "\\n" + window.
A span whose text is empty or only whitespace has no window. Offsets are into the span's own text.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Window:
    window_no: int
    char_start: int
    char_stop: int
    passage: str


def windows(text: str, size: int, overlap: int, min_len: int, prefix: str | None = None) -> list[Window]:
    if not text.strip():
        return []
    width = size if prefix is None else size - len(prefix) - 1
    if width <= overlap:
        raise ValueError("window_too_small")
    bounds: list[tuple[int, int]] = []
    for start in range(0, max(len(text) - overlap, 1), width - overlap):
        stop = min(start + width, len(text))
        if bounds and stop - start < min_len:
            bounds[-1] = (bounds[-1][0], stop)
        else:
            bounds.append((start, stop))
    head = "" if prefix is None else prefix + "\n"
    return [Window(n, a, b, head + text[a:b]) for n, (a, b) in enumerate(bounds)]
