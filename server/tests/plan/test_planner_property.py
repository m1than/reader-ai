# @trace INV-PLAN-1, INV-PLAN-2, INV-PLAN-3
"""Planner invariants under Hypothesis: determinism, coverage with no empty free lesson, size and count bounds,
`fits` and suggestions. [unit]"""

from __future__ import annotations

import datetime as dt
import itertools
import json
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from .conftest import need

START = dt.date(2026, 1, 1)


@st.composite
def inputs(draw: Any, max_pages: int = 5000) -> Any:
    p = need("reader.plan.planner")
    n = draw(st.integers(1, max_pages))
    dense = draw(st.booleans())
    char = st.integers(0, 20000) if dense else st.sampled_from([0, 0, 0, 1, 50, 20000])
    chars = tuple(draw(st.lists(char, min_size=n, max_size=n)))
    if n > 50:  # keep generation fast for long inputs: repeat a drawn block
        block = chars[:50]
        chars = tuple(block[i % 50] for i in range(n))
    spans = tuple(draw(st.integers(1, 60)) if c > 0 else draw(st.integers(0, 2)) for c in chars[:50])
    spans = tuple(spans[i % len(spans)] if chars[i] > 0 else 0 for i in range(n))
    cuts = sorted(draw(st.sets(st.integers(1, n), max_size=10)))
    fixed = []
    for a, b in zip(cuts[::2], cuts[1::2], strict=False):
        fixed.append(p.Fixed(a, b, f"p{a}-{b}"))
    fixed = fixed[:5]
    outline = tuple(
        p.Entry(draw(st.integers(1, 3)), draw(st.one_of(st.none(), st.integers(1, n))))
        for _ in range(draw(st.integers(0, 12)))
    )
    weeks = draw(st.integers(1, 104))
    today = START + dt.timedelta(days=draw(st.integers(-2, weeks * 7 + 3)))
    return p.PlanInput(
        page_chars=chars,
        page_spans=spans,
        outline=outline,
        minutes_per_day=draw(st.integers(2, 48)) * 5,
        total_weeks=weeks,
        rate_centi=draw(st.integers(10, 2000)),
        start_date=START,
        today=today,
        fixed=tuple(fixed),
    )


def _plan(inp: Any) -> Any:
    p = need("reader.plan.planner")
    try:
        return p.plan(inp)
    except p.PlanInputError as e:
        assert (e.code, inp.fixed, sum(inp.page_chars)) == ("no_text", (), 0)
        return None


SETTINGS = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@SETTINGS
@given(inputs())
def test_determinism(inp: Any) -> None:
    p = need("reader.plan.planner")
    a, b = _plan(inp), _plan(inp)
    if a is None:
        return
    assert a == b
    one = json.dumps(p.preimage("ab" * 32, "i", a), sort_keys=True, separators=(",", ":"))
    two = json.dumps(p.preimage("ab" * 32, "i", b), sort_keys=True, separators=(",", ":"))
    assert one == two
    assert a.summary_json() == b.summary_json()


def _free_intervals(inp: Any) -> list[tuple[int, int]]:
    covered = set()
    for f in inp.fixed:
        covered.update(range(f.first, f.last + 1))
    out: list[tuple[int, int]] = []
    page = 1
    n = len(inp.page_chars)
    while page <= n:
        if page in covered:
            page += 1
            continue
        start = page
        while page <= n and page not in covered:
            page += 1
        if sum(inp.page_chars[start - 1 : page - 1]) > 0:
            out.append((start, page - 1))
    return out


@SETTINGS
@given(inputs())
def test_coverage_and_no_empty_free_lesson(inp: Any) -> None:
    result = _plan(inp)
    if result is None:
        return
    lessons = result.lessons
    assert all(a.last < b.first for a, b in itertools.pairwise(lessons))
    planned = {page for x in lessons for page in range(x.first, x.last + 1)}
    skipped = set(range(1, len(inp.page_chars) + 1)) - planned
    covered = {page for f in inp.fixed for page in range(f.first, f.last + 1)}
    free = {page for a, b in _free_intervals(inp) for page in range(a, b + 1)}
    assert skipped == set(range(1, len(inp.page_chars) + 1)) - covered - free
    assert all(inp.page_chars[pg - 1] == 0 for pg in skipped)
    fixed = [(x.first, x.last, x.id) for x in lessons if x.fixed]
    assert fixed == [(f.first, f.last, f.plan_lesson_id) for f in inp.fixed]
    assert all(x.chars > 0 for x in lessons if not x.fixed)


@SETTINGS
@given(inputs())
def test_size_and_count_bounds(inp: Any) -> None:
    p = need("reader.plan.planner")
    result = _plan(inp)
    if result is None:
        return
    intervals = _free_intervals(inp)
    free = [x for x in result.lessons if not x.fixed]
    if not intervals:
        assert free == []
        return
    pages = [pg for a, b in intervals for pg in range(a, b + 1)]
    c = sum(inp.page_chars[pg - 1] for pg in pages)
    big = max(inp.page_chars[pg - 1] for pg in pages)
    nonzero = sum(1 for pg in pages if inp.page_chars[pg - 1] > 0)
    g = len(intervals)
    s = result.summary_json()
    k = max(1, min(s["days_left"] - (g - 1), nonzero))
    for x in free:
        assert x.chars <= -(-c // k) + big
        assert x.milli <= (-(-c // k) + big) * inp.rate_centi // 100
    assert g <= len(free) <= min(k + g - 1, nonzero)
    checks = (
        s["days_left"] >= 1
        and len(free) <= s["days_left"]
        and all(x.milli <= inp.minutes_per_day * 1000 for x in free)
        and all(x.source_chars <= p.LESSON_SOURCE_CHARS_MAX for x in free)
    )
    assert s["fits"] == checks


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(inputs(max_pages=400))
def test_suggestions_fit(inp: Any) -> None:
    p = need("reader.plan.planner")
    import dataclasses

    result = _plan(inp)
    if result is None:
        return
    s = result.summary_json()
    if s["fits"]:
        assert s["suggest"] is None
        return
    m, w = s["suggest"]["minutes_per_day"], s["suggest"]["total_weeks"]
    if m is not None:
        assert (
            m > inp.minutes_per_day
        )
        assert (
            p.plan(dataclasses.replace(inp, minutes_per_day=m)).summary_json()["fits"]
        )
    if w is not None:
        assert w > inp.total_weeks
        assert p.plan(dataclasses.replace(inp, total_weeks=w)).summary_json()["fits"]
