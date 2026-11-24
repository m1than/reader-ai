"""String sanitising (FR-FRESH-8) and the word tokens of INV-FRESH-1 (FR-FRESH-3).

`clean`: NFC; C0/C1 controls, U+200B to U+200F, U+2028/U+2029, the bidi formatting characters of UAX #9
(U+202A to U+202E, U+2066 to U+2069) and lone surrogates (U+D800 to U+DFFF) removed; whitespace collapsed.
Tab, newline and the other whitespace controls become a space before the collapse, so words stay apart.
`cut`: at most a bound, at a word boundary.

`words`: runs of `[^\\W_]+` (Unicode letters and digits; `_` separates), each split further where the script
changes (the first word of the character's Unicode name: LATIN, CYRILLIC, MODIFIER, ...) and between letters
and digits; a character of a script written without spaces (CJK ideographs, kana, Thai, Lao, Myanmar, Khmer)
is a word of its own; a dotted number such as `3.11` or `1.1.1` is one word. Case-folded.
`split`: a token that concatenates words of the non-front spans (an empty joiner, a removed zero-width
character, camelCase) counts as those words.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
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
    + [chr(c) for c in range(0xD800, 0xE000)]
)
_SPACE: Final = re.compile(r"\s+")
# Scripts written without spaces between words: each character is one word.
_NO_SPACE: Final = (
    (0x0E00, 0x0EFF),  # Thai, Lao
    (0x1000, 0x109F),  # Myanmar
    (0x1780, 0x17FF),  # Khmer
    (0x3040, 0x30FF),  # Hiragana, Katakana
    (0x31F0, 0x31FF),  # Katakana phonetic extensions
    (0x3400, 0x4DBF),  # CJK extension A
    (0x4E00, 0x9FFF),  # CJK unified ideographs
    (0xF900, 0xFAFF),  # CJK compatibility ideographs
    (0xFF66, 0xFF9F),  # halfwidth Katakana
    (0x20000, 0x3FFFF),  # CJK extensions B and later
)


def clean(value: str) -> str:
    """NFC, removed characters dropped, whitespace controls as spaces, whitespace collapsed and trimmed."""
    kept = "".join(ch for ch in value if ch not in _REMOVED)  # lone surrogates go before NFC
    normal = unicodedata.normalize("NFC", kept)
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


def has_surrogate(value: str) -> bool:
    """True when the string holds a lone surrogate (U+D800 to U+DFFF), which UTF-8 cannot encode."""
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)


def _no_space(ch: str) -> bool:
    code = ord(ch)
    return any(low <= code <= high for low, high in _NO_SPACE)


def _class(ch: str) -> str:
    if unicodedata.category(ch).startswith("N"):
        return "number"
    return unicodedata.name(ch, "?").split(" ", 1)[0]


def words(value: str) -> list[str]:
    """Case-folded word tokens, the unit of the subject rule and the shingle check (INV-FRESH-1); see the
    module docstring. `[^\\W_]` is `str.isalnum()`."""
    text = unicodedata.normalize("NFC", "".join(ch for ch in value if not has_surrogate(ch)))
    found: list[str] = []
    current: list[str] = []
    kind = ""

    def flush() -> None:
        if current:
            found.append("".join(current).casefold())
            current.clear()

    for i, ch in enumerate(text):
        if ch == "." and kind == "number" and current and i + 1 < len(text) and text[i + 1].isdecimal():
            current.append(ch)  # a dotted number such as 3.11: one word
            continue
        if not ch.isalnum():
            flush()
            continue
        if _no_space(ch):
            flush()
            found.append(ch.casefold())
            continue
        cls = _class(ch)
        if current and cls != kind:
            flush()
        current.append(ch)
        kind = cls
    flush()
    return found


def vocabulary(texts: Iterable[str]) -> frozenset[str]:
    """Every word token of the texts (the non-front spans): the words `split` may cut a token into."""
    return frozenset(w for t in texts for w in words(t))


def split(token: str, vocab: frozenset[str]) -> list[str]:
    """A token that is not itself a word of `vocab` but concatenates its words, as the fewest such words;
    any other token as itself."""
    if not vocab or token in vocab:
        return [token]
    n = len(token)
    longest = min(n, max(len(w) for w in vocab))
    best: list[list[str] | None] = [[], *([None] * n)]
    for end in range(1, n + 1):
        for start in range(max(0, end - longest), end):
            head = best[start]
            if head is None or token[start:end] not in vocab:
                continue
            current = best[end]
            if current is None or len(head) + 1 < len(current):
                best[end] = [*head, token[start:end]]
    whole = best[n]
    return whole if whole else [token]


def expanded(value: str, vocab: frozenset[str]) -> list[str]:
    """`words(value)`, each token cut by `split` into the `vocab` words it concatenates."""
    return [piece for token in words(value) for piece in split(token, vocab)]
