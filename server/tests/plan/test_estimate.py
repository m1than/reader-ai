# @trace FR-PLAN-8, VEC-PLAN-2
"""The compile estimate: Decimal arithmetic from one measured lesson, the run cap, the extrapolation flag. [unit]"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from .conftest import FIXTURES, load_yaml, need

BASIS = load_yaml(FIXTURES / "cost-basis.yaml")


def test_constants() -> None:
    params = need("reader.plan.params")
    c = BASIS["constants"]
    assert params.L02_LOW_USD == Decimal(c["L02_LOW_USD"])
    assert params.L02_HIGH_USD == Decimal(c["L02_HIGH_USD"])
    assert params.L02_KCHARS == Decimal(c["L02_KCHARS"])
    assert params.RUN_CAP_USD == Decimal(c["RUN_CAP_USD"])
    assert params.ESTIMATE_BAND == (Decimal(c["BAND"][0]), Decimal(c["BAND"][1]))
    pipeline_routes = need("reader.pipeline.routes")
    assert params.RUN_CAP_USD == pipeline_routes.RUN_CAP_USD


@pytest.mark.parametrize("case", BASIS["cases"], ids=[c["name"] for c in BASIS["cases"]])
def test_cases(case: dict[str, Any]) -> None:
    estimate = need("reader.plan.estimate")
    chars = case["free_chars"]
    out = estimate.estimate(chars, chars[0] if chars else None)
    expect = case["expect"]
    for key in ("course_low_usd", "course_high_usd", "mean_lesson_kchars", "extrapolated"):
        if key in expect:
            assert out[key] == expect[key], key
    if "first_low_usd" in expect:
        assert out["first_lesson"] == {"low_usd": expect["first_low_usd"], "high_usd": expect["first_high_usd"]}
    assert out["basis"] == "L02" and out["basis_kchars"] == "58.938" and out["run_cap_usd"] == "8.00"


def test_no_float_and_no_first_lesson() -> None:
    estimate = need("reader.plan.estimate")
    out = estimate.estimate([], None)
    assert out["first_lesson"] is None and out["mean_lesson_kchars"] is None and out["extrapolated"] is False
    assert all(not isinstance(v, float) for v in out.values())
