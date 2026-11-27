"""Locator span ids, outline ids and quote hashes (SRV-INGEST FR-INGEST-33, -34; ADR-0039 P2).

Stdlib only: the confined converter, the worker and the tests share it.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

PATH_BYTES_MAX: Final = 1024  # FR-INGEST-33: the non-PDF path field after percent-encoding (structural)

_SHA = r"[0-9a-f]{64}"
_NUM = r"(?:0|[1-9][0-9]{0,9})"
_PDF: Final = re.compile(
    rf"^spn:(?P<sha>{_SHA}):p(?P<page>[1-9][0-9]{{0,4}}):(?P<start>{_NUM})-(?P<end>{_NUM})"
    r"(?:#(?P<ord>0|[1-9][0-9]{0,5}))?$"
)
_FILE: Final = re.compile(
    rf"^spn:(?P<sha>{_SHA}):(?P<path>[^:]+):L(?P<a>[1-9][0-9]{{0,9}})-(?P<b>[1-9][0-9]{{0,9}})$"
)
_LEGACY: Final = re.compile(rf"^{_SHA}:(?:0|[1-9][0-9]{{0,11}})$")
_ESCAPE: Final = re.compile(r"%(25|3A)|%")


class LocatorError(ValueError):
    def __init__(self, code: str = "locator_invalid") -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Locator:
    material_sha256: str
    page: int | None = None
    start: int = 0
    end: int = 0
    ordinal: int | None = None
    path: str | None = None
    line_start: int = 0
    line_end: int = 0


def page_text(texts: Sequence[str]) -> str:
    """The canonical page text: the non-empty span texts in reading order joined by one newline."""
    return "\n".join(t for t in texts if t)


def page_ranges(texts: Sequence[str]) -> list[tuple[int, int, int | None]]:
    """(start, end, ordinal) per span: offsets in code points; an ordinal only for empty spans."""
    return ranges_of_lengths([len(t) for t in texts])


def ranges_of_lengths(lengths: Sequence[int]) -> list[tuple[int, int, int | None]]:
    """page_ranges from the spans' code-point lengths alone (the import keeps lengths, not texts)."""
    starts: list[int | None] = []
    offset = 0
    first = True
    for length in lengths:
        if length:
            if not first:
                offset += 1
            starts.append(offset)
            offset += length
            first = False
        else:
            starts.append(None)
    total = offset
    ranges: list[tuple[int, int, int | None]] = []
    seen: Counter[int] = Counter()
    for i, length in enumerate(lengths):
        start = starts[i]
        if start is not None:
            ranges.append((start, start + length, None))
            continue
        at = next((s for s in starts[i + 1 :] if s is not None), total)
        ranges.append((at, at, seen[at]))
        seen[at] += 1
    return ranges


def pdf_locator(material_sha256: str, page: int, start: int, end: int, ordinal: int | None = None) -> str:
    return format(Locator(material_sha256, page=page, start=start, end=end, ordinal=ordinal))


def page_locators(material_sha256: str, page: int, texts: Sequence[str]) -> list[str]:
    return [pdf_locator(material_sha256, page, s, e, o) for s, e, o in page_ranges(texts)]


def encode_path(path: str) -> str:
    return path.replace("%", "%25").replace(":", "%3A")


def decode_path(encoded: str) -> str:
    def one(match: re.Match[str]) -> str:
        if match.group(1) is None:
            raise LocatorError()
        return "%" if match.group(1) == "25" else ":"

    return _ESCAPE.sub(one, encoded)


def _check_path(path: str) -> None:
    if not path or path.startswith("/") or any(ord(c) < 0x20 or 0x7F <= ord(c) < 0xA0 for c in path):
        raise LocatorError()
    if any(part in ("", "..") for part in path.split("/")):
        raise LocatorError()
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        raise LocatorError() from None
    if len(encode_path(path).encode("utf-8")) > PATH_BYTES_MAX:
        raise LocatorError()


def file_locator(material_sha256: str, path: str, line_start: int, line_end: int) -> str:
    return format(Locator(material_sha256, path=path, line_start=line_start, line_end=line_end))


def format(loc: Locator) -> str:  # the spec names the pair parse/format
    if loc.path is not None:
        _check_path(loc.path)
        if not 1 <= loc.line_start <= loc.line_end:
            raise LocatorError()
        text = f"spn:{loc.material_sha256}:{encode_path(loc.path)}:L{loc.line_start}-{loc.line_end}"
    else:
        suffix = "" if loc.ordinal is None else f"#{loc.ordinal}"
        text = f"spn:{loc.material_sha256}:p{loc.page}:{loc.start}-{loc.end}{suffix}"
    parse(text)
    return text


def parse(span_id: str) -> Locator:
    match = _PDF.fullmatch(span_id)  # fullmatch: `$` alone accepts a trailing newline
    if match is not None:
        start, end = int(match["start"]), int(match["end"])
        ordinal = None if match["ord"] is None else int(match["ord"])
        if start > end or (ordinal is None) == (start == end):
            raise LocatorError()
        return Locator(match["sha"], page=int(match["page"]), start=start, end=end, ordinal=ordinal)
    match = _FILE.fullmatch(span_id)
    if match is None:
        raise LocatorError()
    a, b = int(match["a"]), int(match["b"])
    if a > b:
        raise LocatorError()
    path = decode_path(match["path"])
    _check_path(path)
    if encode_path(path) != match["path"]:
        raise LocatorError()
    return Locator(match["sha"], path=path, line_start=a, line_end=b)


def is_legacy_id(span_id: str) -> bool:
    """FR-INGEST-17's retired `<material_sha256>:<seq>` form, accepted by the resolver and the migration."""
    return _LEGACY.fullmatch(span_id) is not None


def outline_paths(levels: Sequence[int]) -> list[str]:
    """Sibling-ordinal paths: an entry's parent is the nearest preceding entry with a lower level."""
    paths: list[str] = []
    children: dict[int | None, int] = {}  # parent index (None for roots) → children counted so far
    for i, level in enumerate(levels):
        parent = next((j for j in range(i - 1, -1, -1) if levels[j] < level), None)
        children[parent] = children.get(parent, 0) + 1
        prefix = "" if parent is None else paths[parent] + "/"
        paths.append(f"{prefix}{children[parent]}")
    return paths


def outline_id(material_sha256: str, path: str) -> str:
    return f"sec:{material_sha256}:{path}"


def quote_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def equal_hash_ordinals(texts: Sequence[str]) -> list[int]:
    """Each span's 0-based rank among the page's spans with the same quote hash, in reading order."""
    seen: Counter[str] = Counter()
    out: list[int] = []
    for text in texts:
        key = quote_sha256(text)
        out.append(seen[key])
        seen[key] += 1
    return out
