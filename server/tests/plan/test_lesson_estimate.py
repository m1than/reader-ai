# @trace FR-PLAN-13
"""`lesson_estimate(chars)`: the per-lesson compile estimate as a pure function, with hand-computed vectors. [unit]

Band edges: estimate_band 0.5×–2× of basis_kchars 58.938 → 29.469 to 117.876 thousand characters, inclusive.
200,000 characters: low = 200 × 1.2587 / 58.938 = 4.2713… → "4.27"; high = 200 × 2.9714 / 58.938 = 10.08… → the
$8.00 cap.
"""

from __future__ import annotations

import pytest

from .conftest import need

VECTORS = [
    # chars, kchars, low, high, extrapolated
    (29_468, "29.468", None, None, True),
    (29_469, "29.469", None, None, False),
    (117_876, "117.876", None, None, False),
    (117_877, "117.877", None, None, True),
    (58_938, "58.938", "1.26", "2.97", False),
    (200_000, "200.000", "4.27", "8.00", True),
    (0, "0.000", "0.00", "0.00", True),
]


@pytest.mark.parametrize(("chars", "kchars", "low", "high", "extrapolated"), VECTORS)
def test_lesson_estimate_vectors(chars: int, kchars: str, low: str | None, high: str | None, extrapolated: bool) -> None:
    estimate = need("reader.plan.estimate")
    assert hasattr(estimate, "lesson_estimate"), "reader.plan.estimate.lesson_estimate is missing"
    got = estimate.lesson_estimate(chars)
    assert set(got) == {"kchars", "low_usd", "high_usd", "run_cap_usd", "basis", "basis_kchars", "extrapolated"}
    assert got["kchars"] == kchars
    assert got["extrapolated"] is extrapolated
    assert (got["run_cap_usd"], got["basis"], got["basis_kchars"]) == ("8.00", "L02", "58.938")
    if low is not None:
        assert (got["low_usd"], got["high_usd"]) == (low, high)
    # the same numbers as FR-PLAN-8's first-lesson estimate of a one-lesson plan
    first = estimate.estimate([chars], chars)["first_lesson"]
    assert (got["low_usd"], got["high_usd"]) == (first["low_usd"], first["high_usd"])
