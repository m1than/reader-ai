"""The bibliography (FR-FRESH-1): the owner's entry (validated, else `bibliography_invalid` with the field)
and the scan's extraction (out-of-bound fields dropped one by one). Its sha256 is that of its canonical JSON.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date
from typing import Any, Final

from reader.freshness import params
from reader.freshness.text import clean

FIELDS: Final = (
    "title",
    "authors",
    "edition",
    "year",
    "publisher",
    "isbn",
    "original_title",
    "original_authors",
    "original_edition",
    "original_year",
)
STRING_BOUNDS: Final[Mapping[str, int]] = {
    "title": params.BIB_TITLE_CHARS_MAX,
    "edition": params.BIB_EDITION_CHARS_MAX,
    "publisher": params.BIB_PUBLISHER_CHARS_MAX,
    "original_title": params.BIB_TITLE_CHARS_MAX,
    "original_edition": params.BIB_EDITION_CHARS_MAX,
}
YEAR_MIN: Final = 1450
LISTS: Final = ("authors", "original_authors")
YEARS: Final = ("year", "original_year")


class BibliographyInvalid(Exception):
    def __init__(self, field: str) -> None:
        super().__init__("bibliography_invalid")
        self.code = "bibliography_invalid"
        self.field = field


def isbn_ok(value: str) -> bool:
    """ISBN-10 (last character a digit or X) or ISBN-13 with a valid check digit."""
    if len(value) == 10 and value[:9].isdigit() and (value[9].isdigit() or value[9] == "X"):
        total = sum((10 - i) * int(ch) for i, ch in enumerate(value[:9]))
        total += 10 if value[9] == "X" else int(value[9])
        return total % 11 == 0
    if len(value) == 13 and value.isascii() and value.isdigit():
        total = sum(int(ch) * (1 if i % 2 == 0 else 3) for i, ch in enumerate(value[:12]))
        return (10 - total % 10) % 10 == int(value[12])
    return False


def _isbn(value: object) -> str | None:
    if not isinstance(value, str):
        raise ValueError
    normal = clean(value).replace("-", "").replace(" ", "").upper()
    if not normal:
        return None
    if not isbn_ok(normal):
        raise ValueError
    return normal


def _string(field: str, value: object) -> str | None:
    if not isinstance(value, str):
        raise ValueError
    text = clean(value)
    if len(text) > STRING_BOUNDS[field]:
        raise ValueError
    return text or None


def _names(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > params.BIB_AUTHORS_MAX:
        raise ValueError
    names: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError
        name = clean(item)
        if not name or len(name) > params.BIB_AUTHOR_CHARS_MAX:
            raise ValueError
        names.append(name)
    return names


def _year(value: object, today: date) -> int:
    if type(value) is not int or not YEAR_MIN <= value <= today.year + 1:
        raise ValueError
    return value


def _field(field: str, value: object, today: date) -> Any:
    if value is None:
        if field in ("title",):
            raise ValueError
        return [] if field == "authors" else None
    if field in LISTS:
        return _names(value)
    if field in YEARS:
        return _year(value, today)
    if field == "isbn":
        return _isbn(value)
    return _string(field, value)


def canonical(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Every field present, in FIELDS order; absent ones null (authors: [])."""
    out: dict[str, Any] = {}
    for name in FIELDS:
        value = fields.get(name)
        out[name] = [] if name == "authors" and value is None else value
    return out


def sha256(fields: Mapping[str, Any]) -> str:
    data = json.dumps(canonical(fields), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def validate(raw: object, today: date) -> dict[str, Any]:
    """The owner's entry: every field checked; the first bad one raises BibliographyInvalid(field)."""
    if not isinstance(raw, dict):
        raise BibliographyInvalid("body")
    for key in raw:
        if key not in FIELDS:
            raise BibliographyInvalid(str(key)[:40])
    out: dict[str, Any] = {}
    for name in FIELDS:
        try:
            out[name] = _field(name, raw.get(name), today)
        except ValueError:
            raise BibliographyInvalid(name) from None
    if not out["title"]:
        raise BibliographyInvalid("title")
    return canonical(out)


def from_scan(raw: object, today: date) -> dict[str, Any] | None:
    """The scan's extraction: a field out of its bounds is dropped (null); no usable title → None."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    for name in FIELDS:
        try:
            out[name] = _field(name, raw.get(name), today)
        except ValueError:
            out[name] = [] if name == "authors" else None
    if not out.get("title"):
        return None
    return canonical(out)


def with_meta(fields: Mapping[str, Any], source: str | None) -> dict[str, Any]:
    """The read shape: the fields, `source` (unless None) and `sha256`."""
    out = canonical(fields)
    if source is not None:
        out["source"] = source
    out["sha256"] = sha256(fields)
    return out
