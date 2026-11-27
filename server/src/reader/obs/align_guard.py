"""SRV-OBS FR-OBS-41 (INV-OBS-7): the alignment guard, run on every parsed candidate before it is stored and
again on the final text and at export. Verbatim only: a paraphrase passes (review is the gate).

The normaliser is FR-OBS-22's (spikes/obs-stack/check_bench_quotes.py): NFC; U+FFFE, U+00AD and line-end
hyphens joined; lower case; yo to ye (U+0451 to U+0435); words only."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final

from reader.obs import phase2

GUARD_VERSION: Final = "align_guard@1"
_ALIGN: Final = phase2.VEC["align"]
VERBATIM_WORDS: Final[int] = int(_ALIGN["limits"]["verbatim_words"])
MAX_LENGTH_FACTOR: Final[float] = float(_ALIGN["limits"]["max_length_factor"])
MARKERS: Final[frozenset[str]] = frozenset(str(m) for m in _ALIGN["demonstration_markers"])
DELIMITER: Final = "READER-INSTRUCTIONS"
WORD: Final = re.compile(r"[0-9a-zA-Zа-яА-ЯёЁ]+")  # noqa: RUF001 (Cyrillic on purpose)
JOIN: Final = re.compile(r"[￾­]\s*|(?<=\w)-[ \t]*\r?\n\s*")  # markers, soft and line-end hyphens
YO: Final = ("ё", "е")  # noqa: RUF001 (Cyrillic yo to ye)


@dataclass(frozen=True)
class GuardInputs:
    """`base`: the slot's current section; `whole`: labelled queries and answers (checked whole and for
    runs); `verbatim`: card texts, expected facts and material text (checked for runs)."""

    base: str
    whole: tuple[str, ...]
    verbatim: tuple[str, ...]


def _joined(text: str) -> str:
    return JOIN.sub("", unicodedata.normalize("NFC", text))


def words(text: str) -> list[str]:
    return [w.lower().replace(*YO) for w in WORD.findall(_joined(text))]


def _grams(seq: list[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(seq[i : i + n]) for i in range(len(seq) - n + 1)}


def _contains(seq: list[str], part: list[str]) -> bool:
    n = len(part)
    return n > 0 and any(seq[i : i + n] == part for i in range(len(seq) - n + 1))


def guard(proposal: str, inputs: GuardInputs) -> str | None:
    """The refusal code, or None when the proposal passes. Checked in this order: verbatim_8, whole_input,
    demonstration, section_delimiter, no_change."""
    seq = words(proposal)
    mine = _grams(seq, VERBATIM_WORDS)
    for source in (*inputs.whole, *inputs.verbatim):
        if mine & _grams(words(source), VERBATIM_WORDS):
            return "verbatim_8"
    if any(_contains(seq, words(source)) for source in inputs.whole):
        return "whole_input"
    if len(proposal) > MAX_LENGTH_FACTOR * len(inputs.base):
        return "demonstration"
    # Declared limits (code review round 1): words are Latin, Cyrillic and digits only, so text in other
    # scripts is not compared; the markers `q` and `a` also refuse a line that starts with the article "A".
    for line in _joined(proposal).splitlines():  # every line break: \n, \r, U+2028 and the rest
        first = words(line)[:1]
        if first and first[0] in MARKERS:
            return "demonstration"
    if DELIMITER in proposal:
        return "section_delimiter"
    if proposal.strip() == inputs.base.strip():
        return "no_change"
    return None
