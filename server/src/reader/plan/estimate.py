"""The compile estimate (SRV-PLAN FR-PLAN-8): one measured lesson scaled by characters, in Decimal.

An estimate, never a budget: neither end of the range is a bound. Sums are taken unrounded and rounded to
cents half up at the end; the high end of each lesson stops at the run cap.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

from reader.plan.params import ESTIMATE_BAND, L02_HIGH_USD, L02_KCHARS, L02_LOW_USD, RUN_CAP_USD

CENT: Final = Decimal("0.01")
MILLI: Final = Decimal("0.001")


def _low(chars: int) -> Decimal:
    return Decimal(chars) / 1000 * L02_LOW_USD / L02_KCHARS


def _high(chars: int) -> Decimal:
    return min(Decimal(chars) / 1000 * L02_HIGH_USD / L02_KCHARS, RUN_CAP_USD)


def _usd(value: Decimal) -> str:
    return str(value.quantize(CENT, ROUND_HALF_UP))


def estimate(free_chars: Sequence[int], first_chars: int | None) -> dict[str, Any]:
    mean = (
        (Decimal(sum(free_chars)) / len(free_chars) / 1000).quantize(MILLI, ROUND_HALF_UP)
        if free_chars
        else None
    )
    low, high = ESTIMATE_BAND
    return {
        "course_low_usd": _usd(sum((_low(c) for c in free_chars), Decimal(0))),
        "course_high_usd": _usd(sum((_high(c) for c in free_chars), Decimal(0))),
        "first_lesson": (
            None
            if first_chars is None
            else {"low_usd": _usd(_low(first_chars)), "high_usd": _usd(_high(first_chars))}
        ),
        "run_cap_usd": _usd(RUN_CAP_USD),
        "basis": "L02",
        "basis_kchars": str(L02_KCHARS),
        "mean_lesson_kchars": None if mean is None else str(mean),
        "extrapolated": mean is not None and not low * L02_KCHARS <= mean <= high * L02_KCHARS,
    }
