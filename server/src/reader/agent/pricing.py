"""Pricing as data, model resolution, the SDK usage mapping and the cost cross-check (FR-AGENT-6, -8).

`cost(model, usage) = sum(usage_k * rate_k) / 1e6` over the five priced fields, rounded to 6 decimals.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Mapping
from dataclasses import dataclass, fields
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Final

from ruamel.yaml import YAML

from reader.agent.errors import agent_error

PRICING_FILE: Final = Path(__file__).resolve().parent / "pricing.yaml"
# Aliases a reader.yaml task may name, as data next to the pricing table (FR-AGENT-6).
ALIASES: Final[Mapping[str, str]] = {
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",  # FR-AGENT-17
}
# FR-AGENT-17: a request whose prompt (input + cache writes + cache reads) exceeds this many tokens is billed
# in full at the model's `above_100k` tier, when it has one.
TIER_PROMPT_TOKENS: Final = 100_000
# bounds.crosscheck_tolerance: max(relative * sdk_usd, absolute).
TOLERANCE_RELATIVE: Final = Decimal("0.05")
TOLERANCE_ABSOLUTE: Final = Decimal("0.005")
MICRO: Final = Decimal("0.000001")
RATE_KEYS: Final = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read")

log = logging.getLogger("reader.agent")


@dataclass(frozen=True)
class Usage:
    input: int
    output: int
    cache_write_5m: int
    cache_write_1h: int
    cache_read: int
    thinking: int | None = None  # output_tokens_details.thinking_tokens: inside `output`, not priced
    web_search_requests: int | None = None  # priced per search (FR-AGENT-17)

    def as_dict(self) -> dict[str, int]:
        """The priced counts (and `thinking` and `web_search_requests` when the SDK gave them)."""
        return {f.name: int(getattr(self, f.name)) for f in fields(self) if getattr(self, f.name) is not None}

    @property
    def prompt_tokens(self) -> int:
        return self.input + self.cache_write_5m + self.cache_write_1h + self.cache_read


@dataclass(frozen=True)
class Pricing:
    as_of: str
    models: Mapping[str, Mapping[str, Decimal]]
    high_tier: Mapping[str, Mapping[str, Decimal]]  # `above_100k` rows (FR-AGENT-17)
    web_search_usd: Decimal


@functools.cache
def load_pricing() -> Pricing:
    data: Any = YAML(typ="safe").load(PRICING_FILE.read_text(encoding="utf-8"))
    models = {
        str(name): {key: Decimal(str(rates[key])) for key in RATE_KEYS}
        for name, rates in data["models"].items()
    }
    high = {
        str(name): {key: Decimal(str(rates["above_100k"][key])) for key in RATE_KEYS}
        for name, rates in data["models"].items()
        if "above_100k" in rates
    }
    return Pricing(str(data["as_of"]), models, high, Decimal(str(data.get("web_search_usd", 0))))


def resolve_model(name: str) -> str:
    """A task's model (alias or full id) to a priced full id; anything else is `unknown_model`."""
    full = ALIASES.get(name, name)
    if full not in load_pricing().models:
        raise agent_error("unknown_model")
    return full


def rates(model: str, prompt_tokens: int = 0) -> Mapping[str, Decimal]:
    """The model's rates; the `above_100k` tier when it has one and the prompt exceeds TIER_PROMPT_TOKENS."""
    pricing = load_pricing()
    table = pricing.models.get(model)
    if table is None:
        raise agent_error("unknown_model")
    if prompt_tokens > TIER_PROMPT_TOKENS and model in pricing.high_tier:
        return pricing.high_tier[model]
    return table


def micro(value: Decimal) -> Decimal:
    return value.quantize(MICRO, rounding=ROUND_HALF_UP)


def cost(model: str, usage: Usage, *, summed: bool = False) -> Decimal:
    """`summed`: a sum over several requests (model_usage), priced at the lower tier (FR-AGENT-17)."""
    table = rates(model, 0 if summed else usage.prompt_tokens)
    total = sum((Decimal(getattr(usage, key)) * table[key] for key in RATE_KEYS), Decimal(0))
    fee = Decimal(usage.web_search_requests or 0) * load_pricing().web_search_usd
    return micro(total / Decimal(10**6) + fee)


def _count(value: object) -> int:
    if value is None:
        return 0
    if type(value) is not int or value < 0:
        raise agent_error("usage_unmapped")
    return value


def map_usage(sdk_usage: Mapping[str, Any]) -> Usage:
    """ResultMessage.usage to the five priced fields (VEC-AGENT-1's mapping)."""
    if not isinstance(sdk_usage, Mapping):
        raise agent_error("usage_unmapped")
    breakdown = sdk_usage.get("cache_creation")
    created = _count(sdk_usage.get("cache_creation_input_tokens"))
    if breakdown is None:
        if created:
            raise agent_error("usage_unmapped")
        breakdown = {}
    if not isinstance(breakdown, Mapping):
        raise agent_error("usage_unmapped")
    details = sdk_usage.get("output_tokens_details")
    thinking = details.get("thinking_tokens") if isinstance(details, Mapping) else None
    return Usage(
        input=_count(sdk_usage.get("input_tokens")),
        output=_count(sdk_usage.get("output_tokens")),
        cache_write_5m=_count(breakdown.get("ephemeral_5m_input_tokens")),
        cache_write_1h=_count(breakdown.get("ephemeral_1h_input_tokens")),
        cache_read=_count(sdk_usage.get("cache_read_input_tokens")),
        thinking=None if thinking is None else _count(thinking),
    )


# model_usage keys (camelCase, as the SDK passes them through from the CLI).
_MODEL_USAGE_KEYS: Final = {
    "input": "inputTokens",
    "output": "outputTokens",
    "cache_write_5m": "cacheCreationEphemeral5mInputTokens",
    "cache_write_1h": "cacheCreationEphemeral1hInputTokens",
    "cache_read": "cacheReadInputTokens",
}


def map_model_usage(model_usage: Mapping[str, Any]) -> Usage:
    """ResultMessage.model_usage summed over its models: a tool call's usage, nested requests included."""
    if not isinstance(model_usage, Mapping) or not model_usage:
        raise agent_error("usage_unmapped")
    totals = dict.fromkeys(("input", "output", "cache_write_5m", "cache_write_1h", "cache_read", "thinking",
                            "web_search_requests"), 0)
    for entry in model_usage.values():
        if not isinstance(entry, Mapping):
            raise agent_error("usage_unmapped")
        created = _count(entry.get("cacheCreationInputTokens"))
        parts = {key: _count(entry.get(name)) for key, name in _MODEL_USAGE_KEYS.items()}
        if created != parts["cache_write_5m"] + parts["cache_write_1h"]:
            raise agent_error("usage_unmapped")  # the TTL breakdown is missing or inconsistent
        for key, value in parts.items():
            totals[key] += value
        totals["thinking"] += _count(entry.get("thinkingTokens"))
        totals["web_search_requests"] += _count(entry.get("webSearchRequests"))
    return Usage(**totals)


def crosscheck_ok(sdk_usd: Decimal, usd: Decimal) -> bool:
    return abs(sdk_usd - usd) <= max(TOLERANCE_RELATIVE * sdk_usd, TOLERANCE_ABSOLUTE)


def log_crosscheck(model: str, sdk_usd: Decimal, usd: Decimal, summed: Usage | None = None) -> None:
    """`summed`: a tool call's model_usage sum; past TIER_PROMPT_TOKENS on a tiered model the tier is
    unknown (FR-AGENT-17)."""
    if not crosscheck_ok(sdk_usd, usd):
        log.warning("cost_crosscheck model=%s sdk_usd=%s usd=%s", model, sdk_usd, usd)
    if (
        summed is not None
        and model in load_pricing().high_tier
        and summed.prompt_tokens > TIER_PROMPT_TOKENS
    ):
        log.info("tier_unknown model=%s prompt_tokens=%d", model, summed.prompt_tokens)
