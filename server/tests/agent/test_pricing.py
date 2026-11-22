# @trace VEC-AGENT-1, VEC-AGENT-2, FR-AGENT-8
"""Pricing as data, the usage mapping and the cost cross-check (SRV-AGENT FR-AGENT-8)."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import pytest

from .conftest import FIXTURES, load_yaml, need, spec_bounds

VECTORS = load_yaml(FIXTURES / "cost-vectors.yaml")


def test_runtime_pricing_table_equals_the_fixture() -> None:
    pricing = need("reader.agent.pricing")
    assert pricing.PRICING_FILE.read_bytes() == (FIXTURES / "pricing.yaml").read_bytes()
    table = pricing.load_pricing()
    assert set(table.models) == {"claude-opus-5-5", "claude-sonnet-5-5"}
    assert table.models["claude-opus-5-5"]["cache_write_1h"] == Decimal("8.00")


def test_aliases_resolve_and_unknown_models_are_refused() -> None:
    pricing = need("reader.agent.pricing")
    errors = need("reader.agent.errors")
    assert pricing.resolve_model("opus") == "claude-opus-5-5"
    assert pricing.resolve_model("sonnet") == "claude-sonnet-5-5"
    assert pricing.resolve_model("claude-sonnet-5-5") == "claude-sonnet-5-5"
    with pytest.raises(errors.AgentError) as refused:
        pricing.resolve_model("claude-opus-9")
    assert refused.value.code == "unknown_model"


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["name"])
def test_cost_vectors(case: dict[str, Any]) -> None:
    pricing = need("reader.agent.pricing")
    errors = need("reader.agent.errors")
    usage = pricing.Usage(**case["usage"])
    if "error" in case:
        with pytest.raises(errors.AgentError) as refused:
            pricing.cost(case["model"], usage)
        assert refused.value.code == case["error"]
        return
    usd = pricing.cost(case["model"], usage)
    assert usd == Decimal(str(case["usd"])).quantize(Decimal("0.000001"))
    if "sdk_usd" in case:  # every measured stage lies within the tolerance
        assert pricing.crosscheck_ok(Decimal(str(case["sdk_usd"])), usd)


@pytest.mark.parametrize("case", VECTORS["sdk_usage_mapping"], ids=lambda c: c["name"])
def test_sdk_usage_mapping(case: dict[str, Any]) -> None:
    pricing = need("reader.agent.pricing")
    errors = need("reader.agent.errors")
    if "error" in case:
        with pytest.raises(errors.AgentError) as refused:
            pricing.map_usage(case["sdk_usage"])
        assert refused.value.code == case["error"]
        return
    assert pricing.map_usage(case["sdk_usage"]) == pricing.Usage(**case["usage"])


def test_crosscheck_tolerance_is_the_spec_bound(caplog: pytest.LogCaptureFixture) -> None:
    pricing = need("reader.agent.pricing")
    tol = spec_bounds()["crosscheck_tolerance"]
    assert VECTORS["tolerance"] == tol
    assert pricing.crosscheck_ok(Decimal("0.104"), Decimal("0.100"))  # 4 %
    assert not pricing.crosscheck_ok(Decimal("0.110"), Decimal("0.100"))  # 10 %, above $0.005
    assert pricing.crosscheck_ok(Decimal("0.0049"), Decimal("0.0"))  # absolute floor
    caplog.set_level(logging.WARNING)
    pricing.log_crosscheck("claude-opus-5-5", Decimal("0.110"), Decimal("0.100"))
    assert any("cost_crosscheck" in r.getMessage() for r in caplog.records)
