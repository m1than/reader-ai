"""SRV-SEARCH [unit]: the B5 greedy algorithms and the coverage objective (reader.search.greedy).

@trace FR-SEARCH-25 FR-SEARCH-26 FR-SEARCH-27 VEC-SEARCH-6 VEC-SEARCH-7
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from .conftest import fixture, need


def _greedy() -> Any:
    return need("reader.search.greedy")


def _coverage(elements: dict[str, float], covers: dict[str, list[str]]) -> Any:
    return _greedy().Coverage(elements, {c: frozenset(e) for c, e in covers.items()})


@pytest.mark.parametrize("case", fixture("greedy.yaml")["cases"], ids=lambda c: c["name"])
def test_budgeted_vec_search_6(case: dict[str, Any]) -> None:
    greedy = _greedy()
    cards = case["cards"]
    covers = {c: v["covers"] for c, v in cards.items()} | case.get("covers_base", {})
    f = _coverage(case["elements"], covers)
    base = frozenset(case.get("base", []))
    if base:
        f = f.marginal(base)
    picked = greedy.budgeted(
        sorted(cards), f.value, {c: v["cost"] for c, v in cards.items()}, case["budget"],
        {c: v["score"] for c, v in cards.items()}, order=case.get("order"), k=case.get("k"),
    )
    assert picked == case["expect"]


@pytest.mark.parametrize("case", fixture("pivot-cover.yaml")["pivot"])
def test_pivot_vec_search_7(case: dict[str, Any]) -> None:
    pairs = {frozenset(p) for p in case["plus"]}
    clusters = _greedy().pivot(case["cards"], lambda a, b: frozenset((a, b)) in pairs, case["seed"])
    assert clusters == case["expect"]


@pytest.mark.parametrize("case", fixture("pivot-cover.yaml")["set_cover"], ids=lambda c: c["name"])
def test_set_cover_vec_search_7(case: dict[str, Any]) -> None:
    sets = {s: frozenset(v["covers"]) for s, v in case["sets"].items()}
    cost = {s: v["cost"] for s, v in case["sets"].items()}
    assert _greedy().set_cover(case["universe"], sets, cost) == case["expect"]


def test_elements_are_normalised_with_identifier_and_lexeme_weights() -> None:
    greedy = _greedy()
    found = greedy.elements({"bip", "deriv"}, {"BIP-32"}, identifier_weight=2, lexeme_weight=1)
    assert found == {"l:bip": 0.25, "l:deriv": 0.25, "i:bip-32": 0.5}
    assert math.isclose(sum(found.values()), 1.0)
    assert greedy.elements(set(), set(), identifier_weight=2, lexeme_weight=1) == {}


def test_relevance_is_fixed_per_request_and_zero_when_the_top_score_is_not_positive() -> None:
    greedy = _greedy()
    f = greedy.objective({"l:a": 1.0}, {"x": {"l:a"}, "y": set()}, {"x": 0.8, "y": 0.4}, s_top=0.8,
                         relevance_weight=1.0)
    assert math.isclose(f.value(frozenset({"y"})), 0.5)  # λ · 0.4 / 0.8
    assert math.isclose(f.value(frozenset({"x"})), 2.0)  # the element 1 + λ · 1
    other = greedy.objective({"l:a": 1.0}, {"y": set()}, {"y": 0.4}, s_top=0.8, relevance_weight=1.0)
    assert math.isclose(other.value(frozenset({"y"})), 0.5)  # s_top fixed by the request, not by the subset
    for top in (0.0, -0.3):
        flat = greedy.objective({"l:a": 1.0}, {"x": set(), "y": set()}, {"x": -0.3, "y": -0.5}, s_top=top,
                                relevance_weight=1.0)
        assert flat.value(frozenset({"x", "y"})) == 0.0
    negative = greedy.objective({}, {"x": set()}, {"x": -0.1}, s_top=0.5, relevance_weight=1.0)
    assert negative.value(frozenset({"x"})) == 0.0  # max(0, score)


_instances = st.integers(min_value=1, max_value=8).flatmap(
    lambda n: st.tuples(
        st.lists(st.frozensets(st.sampled_from("abcdefgh"), max_size=4), min_size=n, max_size=n),
        st.dictionaries(st.sampled_from("abcdefgh"), st.floats(0, 5), min_size=1),
        st.lists(st.floats(-1, 1), min_size=n, max_size=n),
    )
)


@settings(max_examples=200, deadline=None)
@given(_instances, st.data())
def test_objective_is_monotone_and_submodular(instance: Any, data: st.DataObject) -> None:
    covers, weights, scores = instance
    ids = [f"c{i}" for i in range(len(covers))]
    elements = {f"l:{e}": w for e, w in weights.items()}
    f = _greedy().objective(elements, {i: {f"l:{e}" for e in c} for i, c in zip(ids, covers, strict=True)},
                            dict(zip(ids, scores, strict=True)), s_top=max(scores), relevance_weight=1.0)
    small = frozenset(data.draw(st.sets(st.sampled_from(ids))))
    large = small | frozenset(data.draw(st.sets(st.sampled_from(ids))))
    extra = data.draw(st.sampled_from(ids))
    assert f.value(large) >= f.value(small) - 1e-9
    gain_small = f.value(small | {extra}) - f.value(small)
    gain_large = f.value(large | {extra}) - f.value(large)
    assert gain_small >= gain_large - 1e-9


def test_budgeted_evaluations_are_bounded_on_n_50() -> None:
    greedy = _greedy()
    n = 50
    ids = [f"c{i:02d}" for i in range(n)]
    f = _coverage({f"e{i}": 1.0 + i % 3 for i in range(n)}, {c: [f"e{i}", f"e{(i + 1) % n}"] for i, c in
                                                              enumerate(ids)})
    calls = 0

    def counted(chosen: frozenset[str]) -> float:
        nonlocal calls
        calls += 1
        value: float = f.value(chosen)
        return value

    scores = {c: 1.0 / (i + 1) for i, c in enumerate(ids)}
    greedy.budgeted(ids, counted, dict.fromkeys(ids, 1.0), 1000.0, scores)
    assert calls <= n * (n + 1) // 2 + n
