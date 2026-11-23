"""String sanitising (FR-FRESH-8).

`clean`: NFC; C0/C1 controls, U+200B to U+200F, U+2028/U+2029 and the bidi formatting characters of UAX #9
(U+202A to U+202E, U+2066 to U+2069) removed; whitespace collapsed. Tab, newline and the other whitespace
controls become a space before the collapse, so words stay apart. `cut`: at most a bound, at a word boundary.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

_WHITESPACE_CONTROLS: Final = frozenset(
    chr(c) for c in (0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x1C, 0x1D, 0x1E, 0x1F, 0x85)
)
_REMOVED: Final = frozenset(
    [chr(c) for c in range(0x00, 0x20) if chr(c) not in _WHITESPACE_CONTROLS]
    + [chr(0x7F)]
    + [chr(c) for c in range(0x80, 0xA0) if chr(c) not in _WHITESPACE_CONTROLS]
    + [chr(c) for c in range(0x200B, 0x2010)]
    + [chr(0x2028), chr(0x2029)]
    + [chr(c) for c in range(0x202A, 0x202F)]
    + [chr(c) for c in range(0x2066, 0x206A)]
    + [chr(0xFEFF)]
)
_SPACE: Final = re.compile(r"\s+")


def clean(value: str) -> str:
    """NFC, removed characters dropped, whitespace controls as spaces, whitespace collapsed and trimmed."""
    normal = unicodedata.normalize("NFC", value)
    spaced = "".join(" " if ch in _WHITESPACE_CONTROLS else ch for ch in normal if ch not in _REMOVED)
    return _SPACE.sub(" ", spaced).strip()


def cut(value: str, limit: int) -> str:
    """`value` cut to at most `limit` characters at a word boundary (a hard cut when one word is longer)."""
    if len(value) <= limit:
        return value
    head = value[:limit]
    if not value[limit].isspace():
        boundary = max(head.rfind(" "), head.rfind("\n"), head.rfind("\t"))
        if boundary > 0:
            head = head[:boundary]
    return head.rstrip()


def sanitise(value: str, limit: int) -> str:
    return cut(clean(value), limit)


def has_removed(value: str) -> bool:
    """True when the string holds a character that `clean` removes or turns into a space."""
    return any(ch in _REMOVED or ch in _WHITESPACE_CONTROLS for ch in value)


def words(value: str) -> list[str]:
    """Case-folded word tokens (Unicode `\\w+`), the unit of the shingle check (INV-FRESH-1)."""
    return re.findall(r"\w+", unicodedata.normalize("NFC", value).casefold())
