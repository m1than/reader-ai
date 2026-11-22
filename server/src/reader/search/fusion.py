"""Reciprocal rank fusion, snippets and the OR tsquery text (SRV-SEARCH FR-SEARCH-7, VEC-SEARCH-1)."""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from typing import Final

RRF_K: Final = 60  # Cormack, Clarke, Büttcher 2009 (REF-SEARCH-4)
SNIPPET_CHARS: Final = 300  # structural: a result line, not the chunk


@dataclass(frozen=True)
class Fused[T: Hashable]:
    id: T
    score: float
    dense_rank: int | None
    fts_rank: int | None


def rrf[T: Hashable](dense: Sequence[T], fts: Sequence[T], k: int = RRF_K) -> list[Fused[T]]:
    """score = Σ 1/(k + rank); equal scores: the better dense rank wins, a missing one counts as worst.

    The last key, the position in the input lists, only makes the order total; it is never reached for two
    distinct ids with equal scores and equal dense ranks (the spec's tie rule is complete)."""
    dense_rank = {cid: i + 1 for i, cid in enumerate(dense)}
    fts_rank = {cid: i + 1 for i, cid in enumerate(fts)}
    order = {cid: i for i, cid in enumerate([*dense, *(c for c in fts if c not in dense_rank)])}
    fused = [
        Fused(
            cid,
            (1 / (k + dense_rank[cid]) if cid in dense_rank else 0.0)
            + (1 / (k + fts_rank[cid]) if cid in fts_rank else 0.0),
            dense_rank.get(cid),
            fts_rank.get(cid),
        )
        for cid in order
    ]
    worst = len(dense) + 1
    fused.sort(key=lambda f: (-f.score, f.dense_rank or worst, order[f.id]))
    return fused


def snippet(text: str, limit: int = SNIPPET_CHARS) -> str:
    """The text cut to `limit` characters at the last whitespace at or before it, or hard when none."""
    if len(text) <= limit:
        return text
    head = text[: limit + 1]
    cut = max(head.rfind(" "), head.rfind("\n"), head.rfind("\t"))
    return text[:cut].rstrip() if cut > 0 else text[:limit]


def quote_lexeme(lexeme: str) -> str:
    """tsquery input quoting: double every `'` and every `\\`, then wrap in single quotes."""
    return "'" + lexeme.replace("\\", "\\\\").replace("'", "''") + "'"


def tsquery_text(lexemes: Sequence[str]) -> str:
    """The OR of the quoted lexemes; no operator ever comes from user text."""
    return " | ".join(quote_lexeme(lexeme) for lexeme in lexemes)
