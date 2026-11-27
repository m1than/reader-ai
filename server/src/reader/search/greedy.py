"""SRV-SEARCH FR-SEARCH-25..27: the B5 greedy algorithms over a weighted coverage, pure.

Nothing here enumerates subsets or calls a solver (ADR-0039 invariant 3); the tests hold the exhaustive oracles.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

Value = Callable[[frozenset[str]], float]
_REL: Final = "r:"


@dataclass(frozen=True)
class Coverage:
    """f(S) = the weight of the elements the cards of S cover (monotone submodular for weights ≥ 0)."""

    weights: Mapping[str, float]
    covers: Mapping[str, frozenset[str]]
    base: frozenset[str] = frozenset()

    def _covered(self, ids: Iterable[str]) -> set[str]:
        covered: set[str] = set()
        for card in ids:
            covered |= self.covers.get(card, frozenset())
        return covered

    def value(self, ids: frozenset[str]) -> float:
        covered = self._covered(ids | self.base)
        if self.base:
            covered -= self._covered(self.base)
        return sum(self.weights.get(e, 0.0) for e in covered)

    def marginal(self, base: frozenset[str]) -> Coverage:
        """f(. ∪ base) − f(base): step 7's pass-2 objective with pass 1's output fixed."""
        return Coverage(self.weights, self.covers, self.base | base)


def elements(
    lexemes: Iterable[str], identifiers: Iterable[str], *, identifier_weight: float, lexeme_weight: float
) -> dict[str, float]:
    """FR-SEARCH-25's query elements, weights normalised to sum 1 (empty when there is none)."""
    raw = {f"l:{x}": float(lexeme_weight) for x in lexemes}
    raw.update({f"i:{x.lower()}": float(identifier_weight) for x in identifiers})
    total = sum(raw.values())
    return {} if total <= 0 else {e: w / total for e, w in raw.items()}


def objective(
    weights: Mapping[str, float],
    covers: Mapping[str, Iterable[str]],
    scores: Mapping[str, float],
    *,
    s_top: float,
    relevance_weight: float,
) -> Coverage:
    """Each card also covers its private relevance element, λ · max(0, score) / s_top (0 when s_top ≤ 0)."""
    all_weights = dict(weights)
    all_covers: dict[str, frozenset[str]] = {}
    for card, found in covers.items():
        rel = 0.0 if s_top <= 0 else max(0.0, scores.get(card, 0.0)) / s_top
        all_weights[_REL + card] = relevance_weight * rel
        all_covers[card] = frozenset(found) | {_REL + card}
    return Coverage(all_weights, all_covers)


def _rank(score: Mapping[str, float], card: str) -> tuple[float, str]:
    return (-score.get(card, 0.0), card)


def budgeted(
    cards: Sequence[str],
    f: Value,
    cost: Mapping[str, float],
    budget: float,
    score: Mapping[str, float],
    order: Sequence[str] | None = None,
    k: int | None = None,
) -> list[str]:
    """FR-SEARCH-27 (a): ratio greedy vs the best single card, then the fill; gain greedy when k cannot bind
    together with the budget. Returns the cards in the order they were taken."""
    pool = sorted(set(cards), key=lambda c: _rank(score, c))
    fill = list(order) if order is not None else pool
    by_gain = k is not None and sum(sorted((cost[c] for c in pool), reverse=True)[:k]) <= budget
    chosen: list[str] = []
    spent = 0.0
    current = f(frozenset())
    singles: dict[str, float] = {}
    while k is None or len(chosen) < k:
        best: tuple[tuple[float, float, str], str, float] | None = None
        taken = frozenset(chosen)
        for card in pool:
            if card in taken or spent + cost[card] > budget:
                continue
            value = f(taken | {card})
            if not chosen:
                singles[card] = value
            gain = value - current
            if gain <= 0:
                continue
            key = (-(gain if by_gain else gain / cost[card]) if cost[card] > 0 or by_gain else float("-inf"),
                   -score.get(card, 0.0), card)
            if best is None or key < best[0]:
                best = (key, card, value)
        if best is None:
            break
        chosen.append(best[1])
        spent += cost[best[1]]
        current = best[2]
    if not by_gain and singles:
        star = min(singles, key=lambda c: (-singles[c], *_rank(score, c)))
        if singles[star] > current:
            chosen, spent = [star], cost[star]
    taken_set = set(chosen)
    for card in fill:
        if k is not None and len(chosen) >= k:
            break
        if card in taken_set or card not in cost or spent + cost[card] > budget:
            continue
        chosen.append(card)
        taken_set.add(card)
        spent += cost[card]
    return chosen


def _pivot_key(seed: str, card: str) -> tuple[str, str]:
    return (hashlib.sha256((seed + card).encode("utf-8")).hexdigest(), card)


def pivot_in_order(order: Sequence[str], plus: Callable[[str, str], bool]) -> list[list[str]]:
    """FR-SEARCH-27 (c) over a given order: the first unassigned card takes its unassigned + neighbours."""
    assigned: set[str] = set()
    clusters: list[list[str]] = []
    for card in order:
        if card in assigned:
            continue
        cluster = [card] + [o for o in order if o not in assigned and o != card and plus(card, o)]
        assigned.update(cluster)
        clusters.append(cluster)
    return clusters


def pivot(cards: Sequence[str], plus: Callable[[str, str], bool], seed: str) -> list[list[str]]:
    """The order is sha256(seed ‖ id), seed = the request hash (INV-SEARCH-4: a function of the request only)."""
    return pivot_in_order(sorted(cards, key=lambda c: _pivot_key(seed, c)), plus)


def set_cover(
    universe: Mapping[str, float], sets: Mapping[str, frozenset[str]], cost: Mapping[str, float]
) -> list[str]:
    """FR-SEARCH-27 (b): lowest cost per newly covered weight first, ties by id, until nothing new is coverable."""
    covered: set[str] = set()
    chosen: list[str] = []
    while True:
        best: tuple[float, str] | None = None
        for name in sorted(sets):
            new = sum(universe.get(e, 0.0) for e in sets[name] - covered if e in universe)
            if name in chosen or new <= 0:
                continue
            key = (cost[name] / new, name)
            if best is None or key < best:
                best = key
        if best is None:
            return chosen
        chosen.append(best[1])
        covered |= sets[best[1]]
