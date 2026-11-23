"""Sign-up handle and display-name rules (SRV-AUTH FR-AUTH-25): pure functions, no database."""

from __future__ import annotations

import re
import unicodedata
from typing import Final

from reader.auth.policy import DISPLAY_NAME_MAX, RESERVED_HANDLES

HANDLE_PATTERN: Final = re.compile(r"[a-z][a-z0-9-]{2,31}")
REFUSED_CATEGORIES: Final = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
# The Unicode White_Space property (PropList.txt), as code points. Python's str.strip() would also strip
# U+001C..U+001F, which are Cc and must be refused, not trimmed.
WHITE_SPACE: Final = "".join(
    chr(c)
    for c in (
        *range(0x09, 0x0E), 0x20, 0x85, 0xA0, 0x1680, *range(0x2000, 0x200B), 0x2028, 0x2029, 0x202F,
        0x205F, 0x3000,
    )
)
_DIGITS: Final = str.maketrans("01i", "oll")


def skeleton(handle: str) -> str:
    """Lower-case; remove hyphens; `rn`→`m` and `vv`→`w` left to right; `0`→`o`, `1` and `i`→`l`.

    The two pairs share no letter and their replacements create neither pair, so replacing one after the other
    equals one left-to-right scan without overlap.
    """
    folded = handle.lower().replace("-", "").replace("rn", "m").replace("vv", "w")
    return folded.translate(_DIGITS)


RESERVED_SKELETONS: Final = frozenset(skeleton(h) for h in RESERVED_HANDLES)


def handle_form_ok(handle: object) -> bool:
    """The pattern, no `--`, no trailing `-`. Nothing is normalised: an upper-case letter fails."""
    return (
        isinstance(handle, str)
        and HANDLE_PATTERN.fullmatch(handle) is not None
        and "--" not in handle
        and not handle.endswith("-")
    )


def handle_candidate_ok(handle: object) -> bool:
    """FR-AUTH-25 without the database: well formed, not reserved, no reserved skeleton."""
    return (
        handle_form_ok(handle)
        and isinstance(handle, str)
        and handle not in RESERVED_HANDLES
        and skeleton(handle) not in RESERVED_SKELETONS
    )


def display_name(value: object) -> str | None:
    """The NFC-normalised, trimmed display name, or None when it is not valid (FR-AUTH-25)."""
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFC", value).strip(WHITE_SPACE)
    if not 1 <= len(normalized) <= DISPLAY_NAME_MAX:
        return None
    if any(unicodedata.category(ch) in REFUSED_CATEGORIES for ch in normalized):
        return None
    return normalized
