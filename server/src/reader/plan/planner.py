"""The deterministic planner (SRV-PLAN FR-PLAN-1..7, FR-PLAN-10): pure functions over integers, no I/O.

The free pages (those not in a fixed, started lesson) are spread evenly over the days left before the
deadline by cumulative characters at exact targets; each free lesson holds at least one page with text.
Titles are attached by the caller from outline indices, so no book text passes through here or into the hash.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final

MINUTES_MIN: Final = 10
MINUTES_MAX: Final = 240
MINUTES_STEP: Final = 5
WEEKS_MIN: Final = 1
WEEKS_MAX: Final = 104
WEEK_DAYS: Final = 7
RATE_CENTI_MIN: Final = 10
RATE_CENTI_MAX: Final = 2000
SPAN_HEADER_CHARS: Final = 46  # upper bound of FR-PIPELINE-2's marker line with its joins (42, 45 with joins)
LESSON_SOURCE_CHARS_MAX: Final = 270000  # source_max_chars 300,000 less a 10 % margin (chosen)
TITLE_MAX: Final = 200
EN_DASH: Final = chr(0x2013)  # titles use an en dash between page numbers


class PlanInputError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Entry:
    level: int
    page: int | None


@dataclass(frozen=True)
class Fixed:
    first: int
    last: int
    plan_lesson_id: str


@dataclass(frozen=True)
class PlanInput:
    page_chars: tuple[int, ...]
    page_spans: tuple[int, ...]
    outline: tuple[Entry, ...]
    minutes_per_day: int
    total_weeks: int
    rate_centi: int
    start_date: dt.date
    today: dt.date
    fixed: tuple[Fixed, ...] = ()


@dataclass(frozen=True)
class Lesson:
    id: str
    first: int
    last: int
    chars: int
    spans: int
    source_chars: int
    milli: int
    minutes: int
    fixed: bool
    title_entry: int | None
    title_before: bool


@dataclass(frozen=True)
class Plan:
    input: PlanInput
    today: dt.date
    lessons: tuple[Lesson, ...]
    summary: dict[str, Any] = field(compare=True)

    def summary_json(self) -> dict[str, Any]:
        return dict(self.summary)

    @property
    def free(self) -> tuple[Lesson, ...]:
        return tuple(x for x in self.lessons if not x.fixed)


def weeks_until(start_date: dt.date, target: dt.date) -> int:
    """FR-PLAN-20: the `total_weeks` a client sends for a target date, ceil((d - start + 1) / 7) in 1..104."""
    days = (target - start_date).days + 1
    return min(max(-(-days // 7), WEEKS_MIN), WEEKS_MAX)


def _half_up(milli: int) -> int:
    return (milli + 500) // 1000


def _check(inp: PlanInput) -> None:
    m = inp.minutes_per_day
    if type(m) is not int or not MINUTES_MIN <= m <= MINUTES_MAX or m % MINUTES_STEP:
        raise PlanInputError("minutes_per_day")
    w = inp.total_weeks
    if type(w) is not int or not WEEKS_MIN <= w <= WEEKS_MAX:
        raise PlanInputError("total_weeks")
    r = inp.rate_centi
    if type(r) is not int or not RATE_CENTI_MIN <= r <= RATE_CENTI_MAX:
        raise PlanInputError("rate")
    n = len(inp.page_chars)
    if n < 1 or len(inp.page_spans) != n or any(c < 0 for c in inp.page_chars + inp.page_spans):
        raise PlanInputError("pages")
    previous = 0
    for f in inp.fixed:
        if not previous < f.first <= f.last <= n:
            raise PlanInputError("fixed")
        previous = f.last


def _lesson(
    inp: PlanInput, first: int, last: int, lesson_id: str, fixed: bool, order: Sequence[int] | None
) -> Lesson:
    chars = sum(inp.page_chars[first - 1 : last])
    spans = sum(inp.page_spans[first - 1 : last])
    milli = chars * inp.rate_centi // 100
    entry, before = _title_entry(inp.outline, order, first, last) if order is not None else (None, False)
    return Lesson(
        lesson_id,
        first,
        last,
        chars,
        spans,
        chars + SPAN_HEADER_CHARS * spans,
        milli,
        _half_up(milli),
        fixed,
        entry,
        before,
    )


def _title_entry(
    outline: tuple[Entry, ...], order: Sequence[int], first: int, last: int
) -> tuple[int | None, bool]:
    inside: int | None = None
    before: int | None = None
    for i in order:  # entries with a page, ordered by (page, seq)
        page = outline[i].page or 0
        if page < first:
            before = i
        elif page <= last:
            if inside is None or outline[i].level < outline[inside].level:
                inside = i
        else:
            break
    if inside is not None:
        return inside, False
    return before, before is not None


def lesson_title(lesson: Lesson, titles: Sequence[str]) -> str:
    """FR-PLAN-5: the entry's title, `(pp. a-b)` when the entry lies before the lesson, else `pp. a-b`
    (written with an en dash)."""
    pages = f"{lesson.first}{EN_DASH}{lesson.last}"
    if lesson.title_entry is None:
        out = f"pp. {pages}"
    elif lesson.title_before:
        out = f"{titles[lesson.title_entry]} (pp. {pages})"
    else:
        out = titles[lesson.title_entry]
    return out[:TITLE_MAX]


def _free_intervals(inp: PlanInput) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    page = 1
    n = len(inp.page_chars)
    for f in (*inp.fixed, Fixed(n + 1, n + 1, "")):
        if page < f.first:
            a, b = page, f.first - 1
            if sum(inp.page_chars[a - 1 : b]) > 0:
                out.append((a, b))
        page = f.last + 1
    return out


def _pack(inp: PlanInput, intervals: list[tuple[int, int]], days_left: int) -> list[tuple[int, int]]:
    """FR-PLAN-4: target cuts at ceil(j·C/K), dropped when at or before the previous kept one or when no
    page with text follows in its interval; every interval's last page is a cut."""
    pages = [p for a, b in intervals for p in range(a, b + 1)]
    chars = [inp.page_chars[p - 1] for p in pages]
    ends: list[int] = []  # index in `pages` of each interval's last page
    for a, b in intervals:
        ends.append((ends[-1] if ends else -1) + (b - a + 1))
    # text_after[i]: a page with text follows position i within its interval
    text_after = [False] * len(pages)
    start = 0
    for end in ends:
        seen = False
        for i in range(end, start - 1, -1):
            text_after[i] = seen
            seen = seen or chars[i] > 0
        start = end + 1
    total = sum(chars)
    nonzero = sum(1 for c in chars if c > 0)
    g = len(intervals)
    k = max(1, min(days_left - (g - 1), nonzero))
    cuts = set(ends)
    pos, cum, previous = -1, 0, -1
    for j in range(1, k):
        target = (j * total + k - 1) // k
        while cum < target:
            pos += 1
            cum += chars[pos]
        if pos <= previous or not text_after[pos]:
            continue
        cuts.add(pos)
        previous = pos
    out: list[tuple[int, int]] = []
    start = 0
    for cut in sorted(cuts):
        out.append((pages[start], pages[cut]))
        start = cut + 1
    return out


def _core(inp: PlanInput, titles: bool = True) -> Plan:
    _check(inp)
    today = max(inp.today, inp.start_date)
    days = inp.total_weeks * WEEK_DAYS
    end = inp.start_date + dt.timedelta(days=days - 1)
    days_left = (end - today).days + 1
    intervals = _free_intervals(inp)
    if not intervals and not inp.fixed:
        raise PlanInputError("no_text")
    order = (
        sorted(
            (i for i, e in enumerate(inp.outline) if e.page is not None),
            key=lambda i: (inp.outline[i].page or 0, i),
        )
        if titles
        else None
    )
    ranges = [(a, b, f"p{a}-{b}", False) for a, b in (_pack(inp, intervals, days_left) if intervals else [])]
    ranges += [(f.first, f.last, f.plan_lesson_id, True) for f in inp.fixed]
    lessons = tuple(_lesson(inp, a, b, i, fx, order) for a, b, i, fx in sorted(ranges))
    free = [x for x in lessons if not x.fixed]
    t = inp.minutes_per_day * 1000
    reasons = []
    if days_left < 1 or len(free) > days_left:
        reasons.append("days")
    if any(x.milli > t for x in free):
        reasons.append("minutes")
    if any(x.source_chars > LESSON_SOURCE_CHARS_MAX for x in free):
        reasons.append("source_size")
    n_free = len(free)
    summary: dict[str, Any] = {
        "lesson_count": len(lessons),
        "fixed_count": len(lessons) - n_free,
        "new_count": n_free,
        "study_days": days,
        "start_date": inp.start_date.isoformat(),
        "end": end.isoformat(),
        "days_left": days_left,
        "spare_days": max(0, days_left - n_free),
        "minutes_mean": (sum(x.milli for x in free) + 500 * n_free) // (1000 * n_free) if n_free else None,
        "minutes_max": _half_up(max(x.milli for x in free)) if n_free else None,
        "required_minutes": sum(x.minutes for x in free),
        "finish_by": (today + dt.timedelta(days=n_free - 1)).isoformat() if n_free else None,
        "fits": not reasons,
        "reasons": reasons,
        "suggest": None,
    }
    return Plan(inp, today, lessons, summary)


def _fits(inp: PlanInput) -> bool:
    try:
        return bool(_core(inp, titles=False).summary["fits"])
    except PlanInputError:
        return False


def plan(inp: PlanInput) -> Plan:
    """FR-PLAN-1..7: the plan, its summary and, when it does not fit, the suggestions found by re-running."""
    result = _core(inp)
    if result.summary["fits"]:
        return result
    minutes = next(
        (
            m
            for m in range(inp.minutes_per_day + MINUTES_STEP, MINUTES_MAX + 1, MINUTES_STEP)
            if _fits(replace(inp, minutes_per_day=m))
        ),
        None,
    )
    weeks = next(
        (w for w in range(inp.total_weeks + 1, WEEKS_MAX + 1) if _fits(replace(inp, total_weeks=w))),
        None,
    )
    summary = {**result.summary, "suggest": {"minutes_per_day": minutes, "total_weeks": weeks}}
    return replace(result, summary=summary)


def preimage(material_sha256: str, ingest_id: str, result: Plan) -> dict[str, Any]:
    """FR-PLAN-10: inputs (with the clamped today) and lessons; no titles, no fixed set."""
    inp = result.input
    return {
        "inputs": {
            "material_sha256": material_sha256,
            "ingest_id": ingest_id,
            "rate_centi": inp.rate_centi,
            "total_weeks": inp.total_weeks,
            "minutes_per_day": inp.minutes_per_day,
            "start_date": inp.start_date.isoformat(),
            "today": result.today.isoformat(),
        },
        "lessons": [{"id": x.id, "pages": [x.first, x.last], "minutes": x.minutes} for x in result.lessons],
    }


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def plan_sha256(material_sha256: str, ingest_id: str, result: Plan) -> str:
    return hashlib.sha256(canonical(preimage(material_sha256, ingest_id, result))).hexdigest()
