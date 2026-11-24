# @trace FR-PLAN-21, FR-PLAN-20
"""The manual-order placement of a re-plan as a pure function, and the target date's week count. [unit]"""

from __future__ import annotations

import datetime as dt

from hypothesis import given
from hypothesis import strategies as st

from .conftest import need


def test_placement_cases() -> None:
    order = need("reader.plan.order")
    # kept a, b, c in manual order c, a, b; nothing new
    assert order.placement(["c", "a", "b"], ["a", "b", "c"], {"a", "b", "c"}, set()) == ["c", "a", "b"]
    # a new lesson in front of the plan goes first
    assert order.placement(["c", "a", "b"], ["n", "a", "b", "c"], {"a", "b", "c"}, set()) == ["n", "c", "a", "b"]
    # new lessons follow their plan predecessor; a chain of new lessons stays together
    assert order.placement(["c", "a"], ["a", "x", "y", "c"], {"a", "c"}, set()) == ["c", "a", "x", "y"]
    # removed lessons are gone (b is not in the new plan and not kept)
    assert order.placement(["b", "c", "a"], ["a", "c"], {"a", "c"}, set()) == ["c", "a"]
    # the predecessor is deleted: the nearest visible one counts; deleted lessons go last, in current order
    assert order.placement(["a", "b", "d"], ["a", "d", "b", "n"], {"a", "b", "d"}, {"b", "d"}) == ["a", "n", "b", "d"]
    # every predecessor deleted: first
    assert order.placement(["a"], ["a", "n"], {"a"}, {"a"}) == ["n", "a"]


@st.composite
def _cases(draw: st.DrawFn) -> tuple[list[str], list[str], set[str], set[str]]:
    kept = draw(st.lists(st.sampled_from([f"k{i}" for i in range(8)]), unique=True, max_size=8))
    new = [f"n{i}" for i in range(draw(st.integers(0, 5)))]
    removed = [f"r{i}" for i in range(draw(st.integers(0, 3)))]
    current = draw(st.permutations(kept + removed))
    plan = draw(st.permutations(kept + new))
    deleted = set(draw(st.lists(st.sampled_from(kept), unique=True))) if kept else set()
    return list(current), list(plan), set(kept), deleted


@given(_cases())
def test_placement_properties(case: tuple[list[str], list[str], set[str], set[str]]) -> None:
    order = need("reader.plan.order")
    current, plan, kept, deleted = case
    out = order.placement(current, plan, kept, deleted)
    assert sorted(out) == sorted(plan)  # every lesson of the new plan once, removed ones gone
    visible = [x for x in out if x not in deleted]
    assert out == visible + [x for x in current if x in deleted]  # deleted last, in current order
    assert [x for x in visible if x in kept] == [x for x in current if x in kept and x not in deleted]
    for i, lesson in enumerate(plan):
        if lesson in kept:
            continue
        before = [x for x in plan[:i] if x not in deleted]  # kept-and-visible or new lessons earlier in the plan
        at = visible.index(lesson)
        if before:  # directly after its nearest visible plan predecessor
            assert at > 0 and visible[at - 1] == before[-1]
        else:  # no visible predecessor: first
            assert at == 0


def test_weeks_until() -> None:
    planner = need("reader.plan.planner")
    start = dt.date(2026, 11, 24)
    assert planner.weeks_until(start, start) == 1
    assert planner.weeks_until(start, start + dt.timedelta(days=3)) == 1  # in the first week
    assert planner.weeks_until(start, start + dt.timedelta(days=6)) == 1  # the first week's last day
    assert planner.weeks_until(start, start + dt.timedelta(days=7)) == 2  # on the boundary
    assert planner.weeks_until(start, start + dt.timedelta(days=104 * 7)) == 104  # past 104 weeks: clamped
    assert planner.weeks_until(start, start - dt.timedelta(days=30)) == 1  # before the start: clamped
