"""The estimate and the per-call reserves (FR-FRESH-9, VEC-FRESH-6; REF-FRESH-2 section 4).

high = the scan's reserve from its actual prompt (SRV-AGENT FR-AGENT-9's formula) + `searches_max` times
`search_reserve_floor_usd` + the report's worst case (`report_prompt_tokens_max` at the 1-hour cache-write
rate + `report_output_tokens` at the output rate). low = the expected values (`expected_*` bounds).
cap = `run_cap_usd`. Strings have two decimals: high rounded up to the cent, low to the nearest cent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, ROUND_UP, Decimal
from typing import Final

from reader.agent import ledger
from reader.agent.pricing import load_pricing, micro, rates
from reader.freshness import params

CENT: Final = Decimal("0.01")
MILLION: Final = Decimal(10**6)


@dataclass(frozen=True)
class Estimate:
    low_usd: Decimal
    high_usd: Decimal
    cap_usd: Decimal

    def as_json(self) -> dict[str, str]:
        return {
            "low_usd": str(self.low_usd.quantize(CENT, ROUND_HALF_UP)),
            "high_usd": str(self.high_usd.quantize(CENT, ROUND_UP)),
            "cap_usd": str(self.cap_usd.quantize(CENT)),
        }


def scan_reserve(model: str, prompt_chars: int) -> Decimal:
    out = params.SCAN_OUTPUT_TOKENS
    return ledger.reserve_amount(model, prompt_chars, out, Decimal(0), custom_system=True)


def report_reserve(model: str, prompt_chars: int) -> Decimal:
    out = params.REPORT_OUTPUT_TOKENS
    return ledger.reserve_amount(model, prompt_chars, out, Decimal(0), custom_system=True)


def search_reserve(model: str, prompt_chars: int) -> Decimal:
    """SRV-AGENT's reserve for a tool call (FR-AGENT-17): the worst case plus one search fee, at least the
    floor."""
    worst = ledger.worst_case(model, prompt_chars, params.SEARCH_OUTPUT_TOKENS, custom_system=True)
    return max(params.SEARCH_RESERVE_FLOOR_USD, worst + load_pricing().web_search_usd)


def report_worst(model: str) -> Decimal:
    table = rates(model, params.REPORT_PROMPT_TOKENS_MAX)
    total = Decimal(params.REPORT_PROMPT_TOKENS_MAX) * table["cache_write_1h"]
    total += Decimal(params.REPORT_OUTPUT_TOKENS) * table["output"]
    return micro(total / MILLION)


def _expected(model: str, prompt_tokens: int, output_tokens: int) -> Decimal:
    table = rates(model, prompt_tokens)
    total = Decimal(prompt_tokens) * table["cache_write_1h"] + Decimal(output_tokens) * table["output"]
    return total / MILLION


def estimate(scan_model: str, report_model: str, scan_prompt_chars: int) -> Estimate:
    high = (
        scan_reserve(scan_model, scan_prompt_chars)
        + params.SEARCHES_MAX * params.SEARCH_RESERVE_FLOOR_USD
        + report_worst(report_model)
    )
    scan_tokens = math.ceil(Decimal(scan_prompt_chars) / params.EXPECTED_CHARS_PER_TOKEN)
    low = (
        _expected(scan_model, scan_tokens, params.EXPECTED_SCAN_OUTPUT_TOKENS)
        + params.SEARCHES_MAX * params.EXPECTED_SEARCH_USD
        + _expected(
            report_model, params.EXPECTED_REPORT_PROMPT_TOKENS, params.EXPECTED_REPORT_OUTPUT_TOKENS
        )
    )
    return Estimate(micro(low), high, params.RUN_CAP_USD)
