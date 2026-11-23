# @trace FR-PLAN-1, FR-PLAN-2, FR-PLAN-3, FR-PLAN-4, FR-PLAN-5, FR-PLAN-6, FR-PLAN-7, FR-PLAN-10, VEC-PLAN-1
"""The planner, pure: input checks, minutes, days, packing, titles, summary and suggestions, the plan hash. [unit]"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from typing import Any

import pytest

from .conftest import FIXTURES, load_yaml, need, spec_bounds

CASES = load_yaml(FIXTURES / "planner-cases.yaml")


def _input(case: dict[str, Any]) -> Any:
    p = need("reader.plan.planner")
    d = {**CASES["defaults"], **case}
    if "page_chars_repeat" in d:
        value, count = d["page_chars_repeat"]
        chars = (value,) * count
    else:
        chars = tuple(d["page_chars"])
    fixed = [tuple(f) for f in d.get("fixed", [])]
    if "fixed_singles" in d:
        a, b = d["fixed_singles"]
        fixed += [(n, n, f"p{n}-{n}") for n in range(a, b + 1)]
    return p.PlanInput(
        page_chars=chars,
        page_spans=tuple(1 if c > 0 else 0 for c in chars),
        outline=(),
        minutes_per_day=d["minutes_per_day"],
        total_weeks=d["total_weeks"],
        rate_centi=d["rate_centi"],
        start_date=dt.date.fromisoformat(d["start_date"]),
        today=dt.date.fromisoformat(d.get("today", d["start_date"])),
        fixed=tuple(p.Fixed(a, b, i) for a, b, i in sorted(fixed)),
    )


@pytest.mark.parametrize("case", CASES["cases"], ids=[c["name"] for c in CASES["cases"]])
def test_vector(case: dict[str, Any]) -> None:
    p = need("reader.plan.planner")
    expect = case["expect"]
    if "error" in expect:
        with pytest.raises(p.PlanInputError) as raised:
            p.plan(_input(case))
        assert raised.value.code == expect["error"]
        return
    result = p.plan(_input(case))
    if "lessons" in expect:
        assert [[x.first, x.last] for x in result.lessons] == expect["lessons"]
    if "free_lessons" in expect:
        assert [[x.first, x.last] for x in result.lessons if not x.fixed] == expect["free_lessons"]
    if "ids" in expect:
        assert [x.id for x in result.lessons] == expect["ids"]
    if "minutes" in expect:
        assert [x.minutes for x in result.lessons if not x.fixed] == expect["minutes"]
    summary = result.summary_json()
    for key, value in expect.get("summary", {}).items():
        assert summary[key] == value, key
    if "suggest" in expect:
        assert summary["suggest"] == expect["suggest"]
    if "today_used" in expect:
        assert result.today.isoformat() == expect["today_used"]
    if "same_sha256_as_today" in expect:
        clamped = p.plan(_input({**case, "today": expect["same_sha256_as_today"]}))
        assert p.plan_sha256("ab" * 32, "ingest-1", result) == p.plan_sha256("ab" * 32, "ingest-1", clamped)
        assert p.preimage("ab" * 32, "ingest-1", result)["inputs"]["today"] == expect["same_sha256_as_today"]


def test_bounds_and_input_errors() -> None:
    p = need("reader.plan.planner")
    b = spec_bounds()
    assert (p.MINUTES_MIN, p.MINUTES_MAX, p.MINUTES_STEP) == (10, 240, 5)
    assert (1, 104, b["week_days"]) == (p.WEEKS_MIN, p.WEEKS_MAX, p.WEEK_DAYS)
    assert (
        b["span_header_chars"],
        b["lesson_source_chars_max"],
    ) == (p.SPAN_HEADER_CHARS, p.LESSON_SOURCE_CHARS_MAX)
    base = _input({"page_chars": [1000, 1000, 1000]})
    for field, value, code in (
        ("minutes_per_day", 5, "minutes_per_day"),
        ("minutes_per_day", 245, "minutes_per_day"),
        ("minutes_per_day", 62, "minutes_per_day"),
        ("total_weeks", 0, "total_weeks"),
        ("total_weeks", 105, "total_weeks"),
        ("rate_centi", 9, "rate"),
        ("rate_centi", 2001, "rate"),
        ("fixed", (p.Fixed(2, 4, "p2-4"),), "fixed"),
        ("fixed", (p.Fixed(2, 2, "a"), p.Fixed(1, 1, "b")), "fixed"),
        ("fixed", (p.Fixed(1, 2, "a"), p.Fixed(2, 3, "b")), "fixed"),
        ("fixed", (p.Fixed(3, 2, "a"),), "fixed"),
    ):
        with pytest.raises(p.PlanInputError) as raised:
            p.plan(dataclasses.replace(base, **{field: value}))
        assert raised.value.code == code, (field, value)


def test_minutes_and_source_chars() -> None:
    """FR-PLAN-2: cost = chars × rate_centi // 100 per lesson; minutes rounded half up; 46 header chars a span."""
    p = need("reader.plan.planner")
    inp = dataclasses.replace(_input({"page_chars": [1500, 700]}), rate_centi=264, page_spans=(3, 2))
    lessons = p.plan(inp).lessons
    # 2 pages with text, D 7 → K 2; t1 = 1100 → after p1.
    assert [(x.chars, x.milli, x.minutes) for x in lessons] == [(1500, 3960, 4), (700, 1848, 2)]
    assert [x.source_chars for x in lessons] == [1500 + 46 * 3, 700 + 46 * 2]
    half = p.plan(dataclasses.replace(inp, page_chars=(1894, 0), page_spans=(1, 0))).lessons[0]
    assert (half.milli, half.minutes) == (5000, 5)
    under = p.plan(dataclasses.replace(inp, page_chars=(1704, 0), page_spans=(1, 0))).lessons[0]
    assert (under.milli, under.minutes) == (4498, 4)


def test_titles() -> None:
    """FR-PLAN-5: the shallowest entry inside the lesson, else the last one before it, ordered by (page, seq)."""
    p = need("reader.plan.planner")
    outline = (
        p.Entry(2, 3),  # 0
        p.Entry(1, 1),  # 1
        p.Entry(1, None),  # 2, ignored
        p.Entry(2, 6),  # 3
        p.Entry(1, 6),  # 4 — same page, shallower
    )
    inp = dataclasses.replace(_input({"page_chars": [1000] * 14}), outline=outline)
    result = p.plan(inp)  # K 7: two pages a lesson
    by_range = {(x.first, x.last): (x.title_entry, x.title_before) for x in result.lessons}
    assert by_range[(1, 2)] == (1, False)
    assert by_range[(3, 4)] == (0, False)
    assert by_range[(5, 6)] == (4, False)
    assert by_range[(7, 8)] == (4, True)
    titles = ["Sec 1.1", "Ch 1", "Lost", "Sec 2.1", "Ch 2"]
    assert p.lesson_title(result.lessons[0], titles) == "Ch 1"
    cont = next(x for x in result.lessons if x.first == 7)
    assert p.lesson_title(cont, titles) == "Ch 2 (pp. 7–8)"
    bare = p.plan(dataclasses.replace(inp, outline=()))
    assert p.lesson_title(bare.lessons[0], []) == "pp. 1–2"
    assert len(p.lesson_title(result.lessons[0], ["x" * 300] * 5)) == 200


def test_plan_hash_is_canonical_and_titles_free() -> None:
    """FR-PLAN-10: sha256 of sorted-key compact JSON of {inputs, lessons}; no titles and no fixed set."""
    p = need("reader.plan.planner")
    result = p.plan(_input({"page_chars": [1000, 2000, 3000]}))
    pre = p.preimage("ab" * 32, "ing", result)
    assert set(pre) == {"inputs", "lessons"}
    assert pre["inputs"] == {
        "material_sha256": "ab" * 32,
        "ingest_id": "ing",
        "rate_centi": 100,
        "total_weeks": 1,
        "minutes_per_day": 60,
        "start_date": "2026-01-01",
        "today": "2026-01-01",
    }
    # K 3, C 6000: t1 = 2000 is reached on page 2; t2 = 4000 on page 3, the interval's end → dropped
    assert pre["lessons"] == [
        {"id": "p1-2", "pages": [1, 2], "minutes": 3},
        {"id": "p3-3", "pages": [3, 3], "minutes": 3},
    ]
    raw = json.dumps(pre, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert p.plan_sha256("ab" * 32, "ing", result) == hashlib.sha256(raw).hexdigest()


def test_replan_keeps_fixed_ids() -> None:
    """FR-PLAN-6: fixed lessons keep their plan lesson ids and pages verbatim."""
    p = need("reader.plan.planner")
    result = p.plan(_input({"page_chars": [1000] * 6, "fixed": [[2, 3, "keep-me"]]}))
    assert [(x.id, x.first, x.last, x.fixed) for x in result.lessons if x.fixed] == [("keep-me", 2, 3, True)]
