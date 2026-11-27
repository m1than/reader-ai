"""SRV-SEARCH [unit]: the greedy bounds per pass against exhaustive search on small instances (n ≤ 12).

Enumeration exists only here (ADR-0039 invariant 3).

@trace INV-SEARCH-10
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from .conftest import need

RATIO = (1 - 1 / math.e) / 2
CARDINALITY = 1 - 1 / math.e
ELEMENTS = "abcdefghij"


def _greedy() -> Any:
    return need("reader.search.greedy")


@st.composite
def _instance(draw: st.DrawFn, n_max: int = 12) -> dict[str, Any]:
    n = draw(st.integers(1, n_max))
    ids = [f"c{i:02d}" for i in range(n)]
    weights = {f"l:{e}": draw(st.floats(0.1, 3)) for e in ELEMENTS[: draw(st.integers(1, len(ELEMENTS)))]}
    covers = {i: frozenset(draw(st.sets(st.sampled_from(sorted(weights)), max_size=4))) for i in ids}
    cost = {i: draw(st.floats(0.5, 10)) for i in ids}
    score = {i: draw(st.floats(-1, 1)) for i in ids}
    budget = draw(st.floats(0.5, 25))
    return {"ids": ids, "weights": weights, "covers": covers, "cost": cost, "score": score, "budget": budget}


def _subsets(ids: Sequence[str]) -> Iterator[frozenset[str]]:
    for size in range(len(ids) + 1):
        for combo in itertools.combinations(ids, size):
            yield frozenset(combo)


def _optimum(ids: Sequence[str], f: Callable[[frozenset[str]], float], cost: dict[str, float], budget: float,
             k: int | None = None) -> float:
    return max(
        f(s) for s in _subsets(ids) if sum(cost[c] for c in s) <= budget and (k is None or len(s) <= k)
    )


def _objective(inst: dict[str, Any]) -> Any:
    return _greedy().objective(inst["weights"], inst["covers"], inst["score"],
                               s_top=max(inst["score"].values()), relevance_weight=1.0)


@settings(max_examples=150, deadline=None)
@given(_instance())
def test_budgeted_pass_reaches_its_bound(inst: dict[str, Any]) -> None:
    """The step-5 preselection and step-7 pass 1 are this pass over their own input and budget."""
    f = _objective(inst)
    picked = _greedy().budgeted(inst["ids"], f.value, inst["cost"], inst["budget"], inst["score"])
    assert sum(inst["cost"][c] for c in picked) <= inst["budget"] + 1e-9
    best = _optimum(inst["ids"], f.value, inst["cost"], inst["budget"])
    assert f.value(frozenset(picked)) >= RATIO * best - 1e-9


@settings(max_examples=150, deadline=None)
@given(_instance(), st.data())
def test_pass_two_reaches_its_bound_on_the_marginal_objective(inst: dict[str, Any], data: Any) -> None:
    greedy = _greedy()
    f = _objective(inst)
    first = frozenset(data.draw(st.sets(st.sampled_from(inst["ids"]), max_size=3)))
    rest = [c for c in inst["ids"] if c not in first]
    if not rest:
        return
    marginal = f.marginal(first)
    picked = greedy.budgeted(rest, marginal.value, inst["cost"], inst["budget"], inst["score"])
    best = _optimum(rest, marginal.value, inst["cost"], inst["budget"])
    assert marginal.value(frozenset(picked)) >= RATIO * best - 1e-9


@settings(max_examples=150, deadline=None)
@given(_instance(), st.integers(1, 4))
def test_cardinality_bound_when_the_k_costliest_fit(inst: dict[str, Any], k: int) -> None:
    costliest = sorted(inst["cost"].values(), reverse=True)[:k]
    budget = max(inst["budget"], sum(costliest))
    f = _objective(inst)
    picked = _greedy().budgeted(inst["ids"], f.value, inst["cost"], budget, inst["score"], k=k)
    assert len(picked) <= k
    best = _optimum(inst["ids"], f.value, inst["cost"], budget, k=k)
    assert f.value(frozenset(picked)) >= CARDINALITY * best - 1e-9


@settings(max_examples=150, deadline=None)
@given(_instance(n_max=10))
def test_set_cover_within_h_d(inst: dict[str, Any]) -> None:
    sets = {c: s for c, s in inst["covers"].items() if s}
    if not sets:
        return
    universe = {e: 1.0 for s in sets.values() for e in s}  # Chvátal's H(d) is over unit element weights
    chosen = _greedy().set_cover(universe, sets, inst["cost"])
    assert frozenset().union(*(sets[c] for c in chosen)) == frozenset(universe)
    optimum = min(
        sum(inst["cost"][c] for c in s) for s in _subsets(sorted(sets))
        if frozenset().union(*(sets[c] for c in s)) == frozenset(universe)
    )
    d = max(len(s) for s in sets.values())
    harmonic = sum(1 / i for i in range(1, d + 1))
    assert sum(inst["cost"][c] for c in chosen) <= harmonic * optimum + 1e-9


def _partitions(items: list[str]) -> Iterator[list[list[str]]]:
    if not items:
        yield []
        return
    head, rest = items[0], items[1:]
    for part in _partitions(rest):
        for i in range(len(part)):
            yield [*part[:i], [head, *part[i]], *part[i + 1:]]
        yield [[head], *part]


def _disagreements(clusters: Sequence[Sequence[str]], plus: set[frozenset[str]], ids: Sequence[str]) -> int:
    label = {c: n for n, cluster in enumerate(clusters) for c in cluster}
    return sum(
        (frozenset((a, b)) in plus) != (label[a] == label[b]) for a, b in itertools.combinations(ids, 2)
    )


@settings(max_examples=60, deadline=None)
@given(st.integers(1, 6).flatmap(lambda n: st.tuples(
    st.just(n), st.sets(st.tuples(st.integers(0, n - 1), st.integers(0, n - 1))))))
def test_pivot_expected_disagreements_within_three_times_optimum(drawn: tuple[int, Any]) -> None:
    n, edges = drawn
    ids = [f"c{i}" for i in range(n)]
    plus = {frozenset((ids[a], ids[b])) for a, b in edges if a != b}
    greedy = _greedy()
    orders = list(itertools.permutations(ids))
    total = sum(
        _disagreements(greedy.pivot_in_order(order, lambda a, b: frozenset((a, b)) in plus), plus, ids)
        for order in orders
    )
    optimum = min(_disagreements(p, plus, ids) for p in _partitions(ids))
    assert total / len(orders) <= 3 * optimum + 1e-9
