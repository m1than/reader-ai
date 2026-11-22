"""SRV-TUTOR VEC-TUTOR-7: the bench question set, generated at run time from the compiled lesson revision.

`python bench_questions.py <revision.ipynb>` prints the questions as JSON on stdout for the bench driver. The cell
titles come from the book, so the output is never committed: the bench writes only ids, counts, flags, timings
and costs. The generator stops before any question is asked when the revision has no explain cell or a selected
explain cell has no title (its first markdown heading).
"""

from __future__ import annotations

import json
import math
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

CELL_KIND: Final = "explain"
TAKE: Final = 20
TEMPLATE: Final = "Explain in your own words: {cell_title}"


class BenchError(Exception):
    pass


def _source(cell: dict[str, Any]) -> str:
    source = cell.get("source", "")
    return "".join(source) if isinstance(source, list) else str(source)


def title_of(cell: dict[str, Any]) -> str | None:
    for line in _source(cell).splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip() or None
    return None


def questions(notebook: dict[str, Any]) -> list[tuple[str, str]]:
    """(cell id, question) for the first TAKE explain cells, in notebook order."""
    cells = [
        c
        for c in notebook.get("cells", [])
        if ((c.get("metadata") or {}).get("reader") or {}).get("kind") == CELL_KIND
    ][:TAKE]
    if not cells:
        raise BenchError("no explain cell in the revision")
    out = []
    for cell in cells:
        title = title_of(cell)
        if title is None or not isinstance(cell.get("id"), str):
            raise BenchError(f"explain cell without a title or id: {cell.get('id')!r}")
        out.append((cell["id"], TEMPLATE.format(cell_title=title)))
    return out


def p95(samples: Sequence[float]) -> float:
    """NFR-TUTOR-1: nearest rank, rank ceil(0.95 × n) of the samples sorted ascending."""
    ordered = sorted(samples)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def main(argv: Sequence[str]) -> int:
    notebook = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    try:
        found = questions(notebook)
    except BenchError as error:
        sys.stderr.write(f"bench: {error}\n")
        return 1
    sys.stdout.write(json.dumps([{"cell_id": c, "question": q} for c, q in found], ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
