"""Validation and canonical form of a lesson notebook (SRV-LESSON FR-LESSON-1, FR-LESSON-2; ADR-0004).

The kinds, their fields and the cell type each kind needs are data in cell.schema.json; nothing here names a
kind.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import warnings
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import jsonschema
import nbformat

from reader.ingest import locator

# Structural bounds of the spec (`bounds`), each with its reason there.
INPUT_MAX_BYTES: Final = 16 * 1024 * 1024
NOTEBOOK_MAX_BYTES: Final = 8 * 1024 * 1024
CELLS_MAX: Final = 1000
LESSON_FORMAT: Final = 1
NBFORMAT: Final = 4
NBFORMAT_MINOR_MIN: Final = 5  # cell ids (nbformat 4.5)
# nbformat 4.5's cell id pattern, matched in full (its JSON Schema `$` would also accept a trailing newline).
CELL_ID_RE: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")

SCHEMA: Final[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "cell.schema.json").read_text(encoding="utf-8")
)
CELL_TYPES: Final[dict[str, str]] = SCHEMA["x-cell-type"]
STAGES: Final[frozenset[str]] = frozenset(SCHEMA["$defs"]["provenance"]["properties"]["stage"]["enum"])
_VALIDATOR: Final = jsonschema.Draft202012Validator(
    SCHEMA, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
)


class LessonWriteError(Exception):
    """A refused write; `code` is one of the spec's `write_errors`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Validated:
    notebook: dict[str, Any]
    canonical: bytes
    sha256: str
    cell_count: int


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name}")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("non-finite number")
    return value


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate JSON object key")
    return result


def _is_int(value: object) -> bool:
    return type(value) is int


def _parse(data: bytes) -> dict[str, Any]:
    if len(data) > INPUT_MAX_BYTES:
        raise LessonWriteError("too_large")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise LessonWriteError("not_utf8") from None
    try:
        doc = json.loads(
            text,
            parse_constant=_refuse_constant,
            parse_float=_finite_float,
            object_pairs_hook=_unique_keys,
        )
    except ValueError, RecursionError:
        raise LessonWriteError("not_nbformat") from None
    if not isinstance(doc, dict):
        raise LessonWriteError("not_nbformat")
    major, minor, cells = doc.get("nbformat"), doc.get("nbformat_minor"), doc.get("cells")
    if not (isinstance(major, int) and _is_int(major) and major == NBFORMAT):
        raise LessonWriteError("not_nbformat")
    if not (isinstance(minor, int) and _is_int(minor) and minor >= NBFORMAT_MINOR_MIN):
        raise LessonWriteError("not_nbformat")
    if not isinstance(cells, list) or not all(isinstance(cell, dict) for cell in cells):
        raise LessonWriteError("not_nbformat")
    return doc


def _check_ids(cells: list[dict[str, Any]]) -> None:
    ids = [cell.get("id") for cell in cells]
    if not all(isinstance(i, str) and CELL_ID_RE.fullmatch(i) for i in ids):
        raise LessonWriteError("not_nbformat")
    if len(set(ids)) != len(ids):
        raise LessonWriteError("duplicate_cell_id")


def _nbformat_valid(doc: dict[str, Any]) -> Any:
    """nbformat.validate on a copy; any exception (RecursionError included), warning-driven repair or mutation
    is a refusal."""
    try:
        node: Any = nbformat.from_dict(copy.deepcopy(doc))  # type: ignore[no-untyped-call]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            nbformat.validate(node)
        unchanged = node == doc
    except Exception:
        raise LessonWriteError("not_nbformat") from None
    if not unchanged:
        raise LessonWriteError("not_nbformat")
    return node


def _reader(meta: object) -> dict[str, Any] | None:
    if not isinstance(meta, dict):
        return None
    value = meta.get("reader")
    return value if isinstance(value, dict) else None


def _check_cells(doc: dict[str, Any]) -> None:
    notebook_reader = _reader(doc.get("metadata"))
    lesson_format = None if notebook_reader is None else notebook_reader.get("lesson_format")
    if not (_is_int(lesson_format) and lesson_format == LESSON_FORMAT):
        raise LessonWriteError("missing_reader_metadata")
    metas = [_reader(cell.get("metadata")) for cell in doc["cells"]]
    if any(meta is None for meta in metas):
        raise LessonWriteError("missing_reader_metadata")
    for meta in metas:
        try:
            valid = _VALIDATOR.is_valid(meta)
        except RecursionError:
            raise LessonWriteError("not_nbformat") from None
        if not valid:
            raise LessonWriteError("cell_schema")
        # Step 7b: what the pattern cannot check (start <= end; `#n` present iff the span is empty).
        for span, _ in _span_refs(meta):
            if span.startswith("spn:"):
                try:
                    locator.parse(span)
                except locator.LocatorError:
                    raise LessonWriteError("cell_schema") from None
    for cell, meta in zip(doc["cells"], metas, strict=True):
        if meta is None or CELL_TYPES[meta["kind"]] != cell.get("cell_type"):
            raise LessonWriteError("cell_type_mismatch")


def validate(data: bytes) -> Validated:
    """FR-LESSON-1 in order, then the canonical bytes and their sha256 (FR-LESSON-2)."""
    doc = _parse(data)
    _check_ids(doc["cells"])
    node = _nbformat_valid(doc)
    _check_cells(doc)
    try:
        written: str = nbformat.writes(node)  # type: ignore[no-untyped-call]
    except RecursionError:
        raise LessonWriteError("not_nbformat") from None
    try:
        canonical = written.encode("utf-8")
    except UnicodeEncodeError:
        raise LessonWriteError("not_utf8") from None
    if len(canonical) > NOTEBOOK_MAX_BYTES:
        raise LessonWriteError("too_large")
    if len(doc["cells"]) > CELLS_MAX:
        raise LessonWriteError("too_many_cells")
    # The notebook as stored: parsed from the canonical bytes, so keys nbformat drops on write are gone too.
    stored: dict[str, Any] = json.loads(canonical)
    return Validated(stored, canonical, hashlib.sha256(canonical).hexdigest(), len(doc["cells"]))


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _span_refs(reader: dict[str, Any] | None) -> Iterator[tuple[str, str | None]]:
    """(span, quote_sha256 or None) of the schema's span holders in a validated `metadata.reader`: claim
    citations, cell citations, `source_ref`, `source_refs` and the lab `reference`; never a free-key map
    such as a tradeoff's `rejected_because`."""
    if reader is None:
        return
    holders: list[Any] = [c for claim in _list(reader.get("claims")) if isinstance(claim, dict)
                          for c in _list(claim.get("citations"))]
    holders += _list(reader.get("citations")) + _list(reader.get("source_refs"))
    holders += [reader.get("source_ref"), reader.get("reference")]
    for holder in holders:
        if isinstance(holder, dict) and isinstance(holder.get("span"), str):
            quote = holder.get("quote_sha256")
            yield holder["span"], quote if isinstance(quote, str) else None


def span_refs(notebook: dict[str, Any]) -> list[tuple[str, str | None]]:
    """The notebook's distinct span references in order: citations, source refs, lab refs (FR-LESSON-32)."""
    found = (ref for cell in notebook["cells"] for ref in _span_refs(_reader(cell.get("metadata"))))
    return list(dict.fromkeys(found))


def notes_ids(notebook: dict[str, Any]) -> set[str]:
    """Ids of the cells whose kind is `notes` (learner-owned, INV-LESSON-3)."""
    return {
        cell["id"]
        for cell in notebook["cells"]
        if (_reader(cell.get("metadata")) or {}).get("kind") == "notes"
    }
