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
ALIASES: Final[Mapping[str, str]] = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5"}
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

    def as_dict(self) -> dict[str, int]:
        return {f.name: int(getattr(self, f.name)) for f in fields(self)}


@dataclass(frozen=True)
class Pricing:
    as_of: str
    models: Mapping[str, Mapping[str, Decimal]]


@functools.cache
def load_pricing() -> Pricing:
    data: Any = YAML(typ="safe").load(PRICING_FILE.read_text(encoding="utf-8"))
    models = {
        str(name): {key: Decimal(str(rates[key])) for key in RATE_KEYS}
        for name, rates in data["models"].items()
    }
    return Pricing(str(data["as_of"]), models)


def resolve_model(name: str) -> str:
    """A task's model (alias or full id) to a priced full id; anything else is `unknown_model`."""
    full = ALIASES.get(name, name)
    if full not in load_pricing().models:
        raise agent_error("unknown_model")
    return full


def rates(model: str) -> Mapping[str, Decimal]:
    table = load_pricing().models.get(model)
    if table is None:
        raise agent_error("unknown_model")
    return table


def micro(value: Decimal) -> Decimal:
    return value.quantize(MICRO, rounding=ROUND_HALF_UP)


def cost(model: str, usage: Usage) -> Decimal:
    table = rates(model)
    total = sum((Decimal(getattr(usage, key)) * table[key] for key in RATE_KEYS), Decimal(0))
    return micro(total / Decimal(10**6))


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
    return Usage(
        input=_count(sdk_usage.get("input_tokens")),
        output=_count(sdk_usage.get("output_tokens")),
        cache_write_5m=_count(breakdown.get("ephemeral_5m_input_tokens")),
        cache_write_1h=_count(breakdown.get("ephemeral_1h_input_tokens")),
        cache_read=_count(sdk_usage.get("cache_read_input_tokens")),
    )


def crosscheck_ok(sdk_usd: Decimal, usd: Decimal) -> bool:
    return abs(sdk_usd - usd) <= max(TOLERANCE_RELATIVE * sdk_usd, TOLERANCE_ABSOLUTE)


def log_crosscheck(model: str, sdk_usd: Decimal, usd: Decimal) -> None:
    if not crosscheck_ok(sdk_usd, usd):
        log.warning("cost_crosscheck model=%s sdk_usd=%s usd=%s", model, sdk_usd, usd)
