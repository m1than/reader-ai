"""The spend ledger: reservations, settlements, voids and budgets (SRV-AGENT FR-AGENT-9, INV-AGENT-4).

Every write opens its own connection and commits at once, outside any job transaction (the SRV-EVENTS
amendment), so a rolled-back stage still leaves its spend. `spent(scope)` = settled amounts + reservations
with neither a settlement nor a void.
"""

from __future__ import annotations

import json
import logging
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from reader.agent.errors import AgentError, agent_error
from reader.agent.pricing import Usage, micro, rates

# Bounds (spec `bounds`).
CHARS_PER_TOKEN_FLOOR: Final = Decimal("2.0")
CLI_SYSTEM_TOKENS: Final = 6803
CLI_OVERHEAD_TOKENS: Final = 1000  # beside a custom system prompt (FR-AGENT-13 b)
# pg_advisory_xact_lock(namespace, hashtext(owner)): serializes check-then-reserve per user ("RAGS").
LOCK_NAMESPACE: Final = 0x52414753

log = logging.getLogger("reader.agent")
_WARNED: set[tuple[uuid.UUID, str]] = set()  # (user, UTC month) already warned (budget_warn)


@dataclass(frozen=True)
class Scope:
    owner_id: uuid.UUID
    run_id: uuid.UUID
    lesson_id: uuid.UUID | None
    stage: str
    task: str
    attempt: int


@dataclass(frozen=True)
class Caps:
    run_cap_usd: Decimal
    task_budget_usd: Decimal | None
    monthly_usd: Decimal | None
    warn_at: Decimal | None
    daily_task_usd: Decimal | None = None  # FR-AGENT-12: per user and task, UTC calendar day


@dataclass(frozen=True)
class UpstreamRecord:
    """What agent-proxy saw for one run_token (FR-AGENT-3)."""

    statuses: tuple[int, ...]
    refused: tuple[str, ...]
    in_flight: int = 0  # still being relayed at revoke (code review B2)
    failed: int = 0  # failed after the connection was made: written upstream, outcome unknown

    @property
    def count_2xx(self) -> int:
        return sum(1 for s in self.statuses if 200 <= s < 300)

    @property
    def any_2xx(self) -> bool:
        return self.count_2xx > 0

    def as_json(self) -> dict[str, Any]:
        return {
            "statuses": list(self.statuses),
            "refused": list(self.refused),
            "any_2xx": self.any_2xx,
            "in_flight": self.in_flight,
            "failed": self.failed,
        }

    @classmethod
    def from_json(cls, data: Any) -> UpstreamRecord | None:
        if not isinstance(data, dict):
            return None
        statuses, refused = data.get("statuses"), data.get("refused", [])
        if not isinstance(statuses, list) or not all(type(s) is int for s in statuses):
            return None
        if not isinstance(refused, list) or not all(isinstance(r, str) for r in refused):
            return None
        in_flight, failed = data.get("in_flight", 0), data.get("failed", 0)
        if type(in_flight) is not int or type(failed) is not int:
            return None
        return cls(tuple(statuses), tuple(refused), in_flight, failed)


def worst_case(
    model: str, prompt_chars: int, max_output_tokens: int, *, custom_system: bool = False
) -> Decimal:
    """(CLI tokens + ceil(chars / 2.0)) * the 1h cache-write rate + max output * the output rate."""
    table = rates(model)
    fixed = CLI_OVERHEAD_TOKENS if custom_system else CLI_SYSTEM_TOKENS
    tokens = fixed + math.ceil(Decimal(prompt_chars) / CHARS_PER_TOKEN_FLOOR)
    total = Decimal(tokens) * table["cache_write_1h"] + Decimal(max_output_tokens) * table["output"]
    return micro(total / Decimal(10**6))


def reserve_amount(
    model: str, prompt_chars: int, max_output_tokens: int, floor: Decimal, *, custom_system: bool = False
) -> Decimal:
    return max(floor, worst_case(model, prompt_chars, max_output_tokens, custom_system=custom_system))


def settle_amount(
    reserve: Decimal, record: UpstreamRecord | None, sdk_usd: Decimal | None, *, error_result: bool = False
) -> tuple[Decimal, str | None]:
    """The settled amount, and a WARNING code to log (FR-AGENT-9, VEC-AGENT-1 `settlements`)."""
    if record is None:  # revoke answered 404, failed or was not JSON: the upstream outcome is unknown
        return reserve, None
    if record.in_flight or record.failed:  # a request that may have been billed without our seeing it
        return max(sdk_usd if sdk_usd is not None else Decimal(0), reserve), "outcome_unknown"
    if record.count_2xx == 0:  # nothing was billed
        return Decimal(0), ("settle_mismatch" if sdk_usd is not None and sdk_usd > 0 else None)
    if record.count_2xx > 1:
        return max(sdk_usd if sdk_usd is not None else Decimal(0), reserve), "multiple_2xx"
    if sdk_usd is None or (error_result and sdk_usd == 0):  # an error result's 0 is not a bill
        return reserve, None
    return sdk_usd, None


_SPENT = (
    "SELECT coalesce(sum(s.settled_usd) FILTER (WHERE s.kind = 'settle'), 0)"
    " + coalesce(sum(s.usd) FILTER (WHERE s.kind = 'reserve' AND NOT EXISTS"
    " (SELECT 1 FROM agent_spend c WHERE c.reservation_id = s.id)), 0)"
    " FROM agent_spend s WHERE "
)
_MONTH = (
    "s.owner_id = :o AND s.created_at >= (date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"
)
_DAY = (
    "s.owner_id = :o AND s.task = :t"
    " AND s.created_at >= (date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"
)


async def _spent(
    conn: AsyncConnection,
    *,
    run_id: uuid.UUID | None = None,
    task: str | None = None,
    owner_id: uuid.UUID | None = None,
    month: bool = False,
    day: bool = False,
) -> Decimal:
    params: dict[str, Any]
    if day:
        where, params = _DAY, {"o": owner_id, "t": task}
    elif month:
        where, params = _MONTH, {"o": owner_id}
    elif task is not None:
        where, params = "s.run_id = :r AND s.task = :t", {"r": run_id, "t": task}
    else:
        where, params = "s.run_id = :r", {"r": run_id}
    value = (await conn.execute(text(_SPENT + where), params)).scalar()
    return Decimal(value or 0)


async def spent(
    engine: AsyncEngine,
    *,
    run_id: uuid.UUID | None = None,
    task: str | None = None,
    owner_id: uuid.UUID | None = None,
    month: bool = False,
) -> Decimal:
    async with engine.connect() as conn:
        return await _spent(conn, run_id=run_id, task=task, owner_id=owner_id, month=month)


async def reserve(engine: AsyncEngine, scope: Scope, model: str, usd: Decimal, caps: Caps) -> int:
    """Append a `reserve` row, or refuse before writing anything."""
    async with engine.begin() as conn:
        await conn.execute(
            text("SELECT pg_advisory_xact_lock(:ns, hashtext(:o))"),
            {"ns": LOCK_NAMESPACE, "o": str(scope.owner_id)},
        )
        if await _spent(conn, run_id=scope.run_id) + usd > caps.run_cap_usd:
            raise agent_error("budget_exhausted")
        if caps.task_budget_usd is not None and (
            await _spent(conn, run_id=scope.run_id, task=scope.task) + usd > caps.task_budget_usd
        ):
            raise agent_error("task_budget_exhausted")
        if caps.daily_task_usd is not None and (
            await _spent(conn, owner_id=scope.owner_id, task=scope.task, day=True) + usd > caps.daily_task_usd
        ):
            raise agent_error("daily_budget_exhausted")
        month = await _spent(conn, owner_id=scope.owner_id, month=True)
        if caps.monthly_usd is not None and month + usd > caps.monthly_usd:
            raise agent_error("monthly_budget_exhausted")
        row = await conn.execute(
            text(
                "INSERT INTO agent_spend (owner_id, run_id, lesson_id, stage, task, attempt, kind, model,"
                " usd) VALUES (:o, :r, :l, :s, :t, :a, 'reserve', :m, :u) RETURNING id"
            ),
            {
                "o": scope.owner_id,
                "r": scope.run_id,
                "l": scope.lesson_id,
                "s": scope.stage,
                "t": scope.task,
                "a": scope.attempt,
                "m": model,
                "u": usd,
            },
        )
        reservation_id = int(row.scalar_one())
    if caps.monthly_usd is not None and caps.warn_at is not None:
        threshold = caps.warn_at * caps.monthly_usd
        key = (scope.owner_id, datetime.now(UTC).strftime("%Y-%m"))
        if month + usd >= threshold and key not in _WARNED:  # once per user and month, this process
            _WARNED.add(key)
            log.warning(
                "budget_warn owner=%s month_spent=%s monthly_usd=%s",
                scope.owner_id,
                month + usd,
                caps.monthly_usd,
            )
    return reservation_id


_CLOSE = (
    "INSERT INTO agent_spend (owner_id, run_id, lesson_id, stage, task, attempt, kind, reservation_id, model,"
    " usd, sdk_usd, settled_usd, upstream, input, output, cache_write_5m, cache_write_1h, cache_read)"
    " SELECT owner_id, run_id, lesson_id, stage, task, attempt, CAST(:kind AS text), id, model,"
    " CAST(:usd AS numeric), CAST(:sdk AS numeric), CAST(:settled AS numeric), CAST(:upstream AS jsonb),"
    " CAST(:i AS bigint), CAST(:out AS bigint), CAST(:w5 AS bigint), CAST(:w1 AS bigint), CAST(:cr AS bigint)"
    " FROM agent_spend WHERE id = :rid AND kind = 'reserve'"
)


async def settle(
    engine: AsyncEngine,
    reservation_id: int,
    settled_usd: Decimal,
    sdk_usd: Decimal | None,
    usd: Decimal | None,
    usage: Usage | None,
    upstream: UpstreamRecord | None,
) -> None:
    counts = usage.as_dict() if usage is not None else {}
    params = {
        "kind": "settle",
        "rid": reservation_id,
        "usd": usd if usd is not None else Decimal(0),
        "sdk": sdk_usd,
        "settled": settled_usd,
        "upstream": json.dumps(upstream.as_json()) if upstream is not None else None,
        "i": counts.get("input"),
        "out": counts.get("output"),
        "w5": counts.get("cache_write_5m"),
        "w1": counts.get("cache_write_1h"),
        "cr": counts.get("cache_read"),
    }
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(_CLOSE), params)
    except IntegrityError:
        raise agent_error("settle_refused") from None
    if result.rowcount != 1:
        raise agent_error("settle_refused")


async def void(engine: AsyncEngine, reservation_id: int) -> None:
    """The owner's escape for a reservation a dead worker left open: refused once it has a settlement."""
    params = {
        "kind": "void",
        "rid": reservation_id,
        "usd": Decimal(0),
        "sdk": None,
        "settled": None,
        "upstream": None,
        "i": None,
        "out": None,
        "w5": None,
        "w1": None,
        "cr": None,
    }
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(_CLOSE), params)
    except IntegrityError:
        raise agent_error("void_refused") from None
    if result.rowcount != 1:
        raise agent_error("void_refused")
    log.warning("spend_void reservation=%s", reservation_id)


__all__ = [
    "AgentError",
    "Caps",
    "Scope",
    "UpstreamRecord",
    "reserve",
    "reserve_amount",
    "settle",
    "settle_amount",
    "spent",
    "void",
    "worst_case",
]
