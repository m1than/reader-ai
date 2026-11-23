# @trace VEC-AGENT-1, FR-AGENT-9, FR-AGENT-6, FR-AGENT-10
"""Settlement and error classification from the measured error shapes (VEC-AGENT-1 `settlements`), and the
worst-case reserve formula (FR-AGENT-9)."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

import pytest

from .conftest import FIXTURES, load_yaml, need, spec_bounds

VECTORS = load_yaml(FIXTURES / "cost-vectors.yaml")


def _record(upstream: dict[str, Any] | None) -> Any:
    ledger = need("reader.agent.ledger")
    if upstream is None:
        return None
    return ledger.UpstreamRecord(
        statuses=tuple(upstream["statuses"]),
        refused=tuple(upstream.get("refused", ())),
        in_flight=upstream.get("in_flight", 0),
        failed=upstream.get("failed", 0),
    )


def _error_result(case: dict[str, Any]) -> bool:
    return bool(case.get("error_result", case.get("sdk_error") is not None))


@pytest.mark.parametrize("case", VECTORS["settlements"], ids=lambda c: c["name"])
def test_settlement_vectors(case: dict[str, Any]) -> None:
    ledger = need("reader.agent.ledger")
    record = _record(case["upstream"])
    if record is not None:
        assert record.any_2xx is case["upstream"]["any_2xx"]
        assert ledger.UpstreamRecord.from_json(record.as_json()) == record
    sdk = None if case["sdk_usd"] is None else Decimal(str(case["sdk_usd"]))
    settled, log_code = ledger.settle_amount(
        Decimal(str(case["reserve"])), record, sdk, error_result=_error_result(case)
    )
    assert settled == Decimal(str(case["settled_usd"]))
    assert log_code == case.get("log")


@pytest.mark.parametrize("case", VECTORS["settlements"], ids=lambda c: c["name"])
def test_error_classification_vectors(case: dict[str, Any]) -> None:
    provider = need("reader.agent.provider")
    record = _record(case["upstream"])
    sdk = None if case["sdk_usd"] is None else Decimal(str(case["sdk_usd"]))
    code = provider.classify(
        assistant_error=case.get("sdk_error"),
        is_error=_error_result(case),
        sdk_usd=sdk,
        record=record,
    )
    assert code == case.get("error")


def test_transient_and_permanent_codes() -> None:
    errors = need("reader.agent.errors")
    for code in ("provider_unavailable", "rate_limited", "output_invalid"):
        assert isinstance(errors.agent_error(code), errors.TransientAgentError)
    for code in ("sdk_error", "tools_mismatch", "unknown_model", "provider_missing", "usage_unmapped"):
        error = errors.agent_error(code)
        assert isinstance(error, errors.AgentError)
        assert not isinstance(error, errors.TransientAgentError)
    for code in ("budget_exhausted", "task_budget_exhausted", "monthly_budget_exhausted"):
        assert isinstance(errors.agent_error(code), errors.BudgetRefused)


@pytest.mark.parametrize(
    ("model", "chars", "max_out", "floor"),
    [
        ("claude-opus-5-5", 60034, 32000, Decimal("0.61")),  # L02 draft: the estimate is above the floor
        ("claude-sonnet-5-5", 100, 16000, Decimal("0.25")),  # a tiny prompt: the floor wins
        ("claude-opus-5-5", 300000, 32000, Decimal("0")),
    ],
)
def test_worst_case_reserve(model: str, chars: int, max_out: int, floor: Decimal) -> None:
    ledger = need("reader.agent.ledger")
    pricing = need("reader.agent.pricing")
    rates = pricing.load_pricing().models[model]
    b = spec_bounds()
    tokens = b["cli_system_tokens"] + math.ceil(chars / b["chars_per_token_floor"])
    expected = (Decimal(tokens) * rates["cache_write_1h"] + Decimal(max_out) * rates["output"]) / Decimal(
        10**6
    )
    expected = expected.quantize(Decimal("0.000001"))
    assert ledger.worst_case(model, chars, max_out) == expected
    assert ledger.reserve_amount(model, chars, max_out, floor) == max(floor, expected)
