"""The `X-Reader-Filename` header: display metadata only, never a path, never logged (FR-INGEST-18)."""

from __future__ import annotations

import unicodedata
from typing import Final
from urllib.parse import unquote_to_bytes

MAX_CHARS: Final = 200
_HEX: Final = frozenset("0123456789abcdefABCDEF")
_BIDI: Final = frozenset({0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)})


class FilenameError(ValueError):
    """400 `filename`; carries no part of the header."""


def _strict_escapes(raw: str) -> None:
    i = 0
    while i < len(raw):
        if raw[i] == "%":
            escape = raw[i + 1 : i + 3]
            if len(escape) < 2 or not set(escape) <= _HEX:
                raise FilenameError
            i += 3
        else:
            if not raw[i].isascii():
                raise FilenameError
            i += 1


def _kept(ch: str) -> bool:
    code = ord(ch)
    return not (code < 0x20 or 0x7F <= code <= 0x9F or ch in "/\\" or code in _BIDI)


def clean(raw: str) -> str:
    """Strict percent-decoding (RFC 3986), UTF-8, NFC, then controls, separators and bidi controls removed."""
    _strict_escapes(raw)
    try:
        text = unquote_to_bytes(raw).decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise FilenameError from None
    text = "".join(ch for ch in unicodedata.normalize("NFC", text) if _kept(ch))
    if not 1 <= len(text) <= MAX_CHARS:
        raise FilenameError
    return text
