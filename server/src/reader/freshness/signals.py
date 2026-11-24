"""The dated-claim score (FR-FRESH-2): fixed patterns and a fixed word list per language, as data.

score(text) = the number of pattern matches + the occurrences of each listed word stem (case-folded). English
stems always count (technical terms in other languages are often English); a language's own list is added.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

PATTERNS: Final = (
    re.compile(r"\d+\.\d+"),  # version numbers
    re.compile(r"(?<!\d)(?:199\d|20\d\d)(?!\d)"),  # four-digit years 1990 to 2099
)
WORDS: Final[Mapping[str, tuple[str, ...]]] = {
    "en": ("version", "latest", "deprecated", "release", "obsolete", "replaced", "current"),
    "ru": ("версия", "версии", "последн", "устарел", "выпуск", "обновл", "актуальн", "заменен"),
}


def language(value: str | None) -> str:
    """The base language tag (`ru-RU` gives `ru`), lower-cased; `en` when absent."""
    return (value or "en").split("-")[0].split("_")[0].lower() or "en"


def word_list(lang: str) -> tuple[str, ...]:
    base = language(lang)
    own = WORDS.get(base, ()) if base != "en" else ()
    return WORDS["en"] + own


def score(text: str, lang: str) -> int:
    folded = text.casefold()
    return sum(len(p.findall(text)) for p in PATTERNS) + sum(folded.count(w) for w in word_list(lang))
