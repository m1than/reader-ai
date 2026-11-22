"""The model's compact notebook form and the lesson notebook built from it (SRV-PIPELINE FR-PIPELINE-3, -7).

The model writes `{"cells": [{id, kind, source, sectors?, <kind fields>}]}` with citations as
`{"span": "s<k>"}` or `{"url", "retrieved"}`. Code expands aliases (page and title from the span, never from
the model), stamps each cell's provenance, carries over the previous revision's `notes` cells, and validates
the result with SRV-LESSON's schema; anything invalid is `output_invalid`.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from reader.lessons.schema import CELL_TYPES, LessonWriteError, validate
from reader.pipeline.findings import Problem
from reader.pipeline.findings import slice_items as findings_slice_items
from reader.pipeline.reports import Key, expand_citations, invalid, parse_json
from reader.pipeline.source import Span

CELL_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
BASE_KEYS: Final = frozenset({"id", "kind", "source", "sectors"})
CITING_KINDS: Final = frozenset({"explain", "changed", "erratum"})


@dataclass(frozen=True)
class Built:
    notebook: dict[str, Any]
    stripped: frozenset[Key] = field(default_factory=frozenset)


def parse(text: str) -> Any:
    return parse_json(text)


def _ref(value: Any, aliases: Mapping[str, Span]) -> dict[str, Any] | None:
    alias = value.get("span") if isinstance(value, dict) else None
    span = aliases.get(alias) if isinstance(alias, str) else None
    if span is None:
        return None
    material = span.span_id.split(":", 1)[0]
    return {"material_sha256": material, "page": span.page, "span": span.span_id}


def _reader(
    cell: dict[str, Any],
    aliases: Mapping[str, Span],
    title: str,
    provenance: dict[str, Any],
    stripped: set[Key],
) -> dict[str, Any]:
    kind = cell["kind"]
    reader: dict[str, Any] = {"kind": kind, "sectors": cell.get("sectors", []), "provenance": provenance}
    for key, value in cell.items():
        if key not in BASE_KEYS and key != "verified":  # `verified` is written by lab verification only
            reader[key] = copy.deepcopy(value)
    if kind == "explain":
        for claim in reader.get("claims", []):
            if not isinstance(claim, dict):
                raise invalid()
            kept, dropped = expand_citations(claim.get("citations", []), aliases, title)
            claim["citations"] = kept
            if dropped:
                stripped.add((str(cell["id"]), str(claim.get("id"))))
    elif kind in CITING_KINDS:
        reader["citations"], dropped = expand_citations(reader.get("citations", []), aliases, title)
    if kind == "erratum":
        reader["source_ref"] = _ref(reader.get("source_ref"), aliases)
    if kind == "route":
        refs = [_ref(r, aliases) for r in reader.get("source_refs", [])]
        reader["source_refs"] = [r for r in refs if r is not None]
    if kind == "open":
        reader.setdefault("status", "open")
        reader.setdefault("answer_ref", None)
    return reader


def build(
    compact: Any,
    *,
    title: str,
    aliases: Mapping[str, Span],
    material_title: str,
    stage: str,
    prompt_version: str,
    model: str,
    previous: Mapping[str, Any] | None,
) -> Built:
    cells_in = compact.get("cells") if isinstance(compact, dict) else None
    if not isinstance(cells_in, list) or not cells_in:
        raise invalid()
    provenance = {"stage": stage, "prompt_version": prompt_version, "model": model}
    stripped: set[Key] = set()
    cells: list[dict[str, Any]] = []
    for cell in cells_in:
        if not isinstance(cell, dict) or cell.get("kind") not in CELL_TYPES:
            raise invalid()
        cid, source = cell.get("id"), cell.get("source", "")
        if not isinstance(cid, str) or not CELL_ID.fullmatch(cid) or not isinstance(source, str):
            raise invalid()
        out: dict[str, Any] = {
            "id": cid,
            "cell_type": CELL_TYPES[cell["kind"]],
            "metadata": {"reader": _reader(cell, aliases, material_title, dict(provenance), stripped)},
            "source": source,
        }
        if out["cell_type"] == "code":
            out["execution_count"] = None
            out["outputs"] = []
        cells.append(out)
    ids = {c["id"] for c in cells}
    for cell in (previous or {}).get("cells", []):
        if cell.get("metadata", {}).get("reader", {}).get("kind") == "notes" and cell["id"] not in ids:
            cells.append(copy.deepcopy(cell))  # SRV-LESSON INV-LESSON-3: notes cells are never removed
    notebook = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python"},
            "reader": {"lesson_format": 1, "title": title},
        },
        "cells": cells,
    }
    try:
        validated = validate(to_bytes(notebook))
    except LessonWriteError, ValueError:
        raise invalid() from None
    return Built(validated.notebook, frozenset(stripped))


def to_bytes(notebook: Mapping[str, Any]) -> bytes:
    return json.dumps(notebook, ensure_ascii=False).encode("utf-8")


def to_compact(notebook: Mapping[str, Any], aliases: Mapping[str, Span]) -> dict[str, Any]:
    """The model's view of a stored notebook: span ids back to aliases, provenance and `verified` removed."""
    back = {span.span_id: alias for alias, span in aliases.items()}

    def cite(c: Any) -> Any:
        if isinstance(c, dict) and "span" in c:
            return {"span": back.get(c["span"], "unknown")}
        return c

    cells = []
    for cell in notebook.get("cells", []):
        reader = dict(cell.get("metadata", {}).get("reader", {}))
        reader.pop("provenance", None)
        reader.pop("verified", None)
        if "claims" in reader:
            reader["claims"] = [
                {**c, "citations": [cite(x) for x in c.get("citations", [])]} for c in reader["claims"]
            ]
        if "citations" in reader:
            reader["citations"] = [cite(x) for x in reader["citations"]]
        if isinstance(reader.get("source_ref"), dict):
            reader["source_ref"] = cite(reader["source_ref"])
        if "source_refs" in reader:
            reader["source_refs"] = [cite(x) for x in reader["source_refs"]]
        source = cell.get("source", "")
        cells.append(
            {"id": cell["id"], **reader, "source": "".join(source) if isinstance(source, list) else source}
        )
    return {"cells": cells}


def slice_items(notebook: Mapping[str, Any]) -> list[Problem]:
    return findings_slice_items(notebook)
