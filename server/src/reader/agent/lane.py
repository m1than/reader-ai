"""The `judge.find` job on the worker-judge lane (SRV-AGENT FR-AGENT-21; ADR-0039 B6, B7b).

One job, one call: read the request by its id, refuse what cannot be judged in time or within the input
cap, reuse a recorded output, else make exactly one Provider call (today's per-call path: reserve, issue,
one CLI, revoke, settle), parse it by ordinals, and record it under the pack guard. The request is always
deleted and the waiter notified, in its own transaction, at the end. Query and card text stay in the
request; nothing logs them.

Seams until their specs land: `JudgeRequests` (SRV-EVENTS's `judge_requests` table), `PackGuard` (SRV-INGEST's
per-pack locks; default: always active while no purge exists), `JudgeInputsSink` (SRV-OBS; default: no copy).
The kind joins KIND_MODULES with the table (V.kl-deploy-judge-lane).
"""

from __future__ import annotations

import contextlib
import functools
import logging
import math
import os
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Protocol

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from reader.agent import judges
from reader.agent.errors import AgentError, agent_error
from reader.agent.ledger import CHARS_PER_TOKEN_FLOOR
from reader.agent.pricing import resolve_model
from reader.agent.provider import PROXY_URL, HttpProxyClient, Provider, StageCall
from reader.config import ReaderConfig, load_config
from reader.jobs.model import JobContext, JobKind

# Bounds (spec `bounds`).
JUDGE_KILL_FACTOR: Final = 3
LANE_TOKEN_TTL_S: Final = 30.0
LANE_TTL_MARGIN_S: Final = 5.0
JUDGE_FIND_INPUT_TOKENS_MAX: Final = 3000
LANE_ISSUE_TIMEOUT_S: Final = 1.0
KIND: Final = "judge.find"
QUEUE: Final = "judge"
NOTIFY_CHANNEL: Final = "judge_done"
KEY_RETENTION_S: Final = 86_400  # SRV-EVENTS FR-EVENTS-11 (amended): a request-id key is never reused
DEFAULT_CONFIG: Final = "/etc/reader/reader.yaml"  # OPS-CLI's read-only bind (READER_CONFIG)

log = logging.getLogger("reader.agent.lane")


class JudgeFindArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: uuid.UUID
    input_sha256: str


@dataclass(frozen=True)
class JudgeRequest:
    """A `judge_requests` row (SRV-EVENTS): the only place the query and card text live."""

    request_id: uuid.UUID
    owner_id: uuid.UUID
    line: str
    run_id: uuid.UUID
    run_cap_usd: Decimal
    lesson_id: uuid.UUID | None
    daily_budget_usd: Decimal | None
    created_at: datetime
    deadline: datetime
    query: str
    cards: tuple[tuple[str, str], ...]
    pack_ids: tuple[str, ...]


class JudgeRequests(Protocol):
    async def get(self, conn: AsyncConnection, request_id: uuid.UUID) -> JudgeRequest | None: ...

    async def delete(self, conn: AsyncConnection, request_id: uuid.UUID) -> None: ...


class PackGuard(Protocol):
    async def lock_active(self, conn: AsyncConnection, pack_ids: Sequence[str]) -> bool: ...


class JudgeInputsSink(Protocol):
    async def copy(self, conn: AsyncConnection, request: JudgeRequest, output: Mapping[str, Any]) -> None: ...


class AlwaysActive:
    """The production guard while no pack purge exists; SRV-INGEST switches it to its per-pack lock check."""

    async def lock_active(self, conn: AsyncConnection, pack_ids: Sequence[str]) -> bool:
        return True


class NoSink:
    """The production sink until SRV-OBS supplies the `judge_inputs` copy."""

    async def copy(self, conn: AsyncConnection, request: JudgeRequest, output: Mapping[str, Any]) -> None:
        return None


@dataclass(frozen=True)
class Deps:
    provider: Callable[[AsyncEngine], AbstractAsyncContextManager[Provider]]
    config: Callable[[], ReaderConfig]
    requests: JudgeRequests
    guard: PackGuard = field(default_factory=AlwaysActive)
    sink: JudgeInputsSink = field(default_factory=NoSink)


def proxy_client() -> HttpProxyClient:
    """The lane's agent-proxy client: a slow issue degrades `find` instead of eating its budget."""
    return HttpProxyClient(PROXY_URL, timeout_s=LANE_ISSUE_TIMEOUT_S)


async def _now(engine: AsyncEngine) -> datetime:
    async with engine.connect() as conn:
        value: datetime = (await conn.execute(text("SELECT clock_timestamp()"))).scalar_one()
    return value


async def _handle(engine: AsyncEngine, owner_id: uuid.UUID) -> str | None:
    async with engine.connect() as conn:
        value = (await conn.execute(text("SELECT handle FROM users WHERE id = :o"), {"o": owner_id})).scalar()
    return value if isinstance(value, str) else None


async def judge_find(deps: Deps, args: JudgeFindArgs, conn: AsyncConnection, ctx: JobContext) -> None:
    request = await deps.requests.get(conn, args.request_id)
    if request is None:
        log.info("judge_skip request=%s reason=no_request", args.request_id)
        return
    engine = conn.engine
    try:
        await _judge(deps, request, args.input_sha256, engine)
    finally:
        async with engine.begin() as end:
            await deps.requests.delete(end, request.request_id)
            await end.execute(
                text("SELECT pg_notify(:c, :p)"), {"c": NOTIFY_CHANNEL, "p": str(request.request_id)}
            )


async def _judge(deps: Deps, request: JudgeRequest, input_sha256: str, engine: AsyncEngine) -> None:
    rid = request.request_id
    if request.line not in judges.LANE_LINES:
        log.info("judge_skip request=%s reason=line_invalid", rid)
        return
    now = await _now(engine)
    if now >= request.deadline:
        log.info("judge_skip request=%s reason=deadline", rid)
        return
    cards = list(request.cards)
    if judges.find_input_sha256(request.owner_id, request.query, cards) != input_sha256:
        log.warning("judge_input_mismatch request=%s", rid)
        return
    window = (request.deadline - request.created_at).total_seconds() * JUDGE_KILL_FACTOR
    if window > LANE_TOKEN_TTL_S - LANE_TTL_MARGIN_S:
        raise agent_error("judge_window_too_long")
    prompt = judges.render_find_input(request.query, cards)
    if math.ceil(Decimal(len(prompt)) / CHARS_PER_TOKEN_FLOOR) > JUDGE_FIND_INPUT_TOKENS_MAX:
        raise agent_error("judge_input_too_large")
    config = deps.config()
    task = config.tasks.get(judges.FIND)
    if task is None:
        raise agent_error("unknown_model")
    judge = judges.find_judge(resolve_model(task.model))
    async with engine.connect() as read:
        if await judges.recorded(read, request.owner_id, input_sha256, judge) is not None:
            log.info("judge_skip request=%s reason=recorded", rid)
            return
    handle = await _handle(engine, request.owner_id)
    if handle is None:
        log.info("judge_skip request=%s reason=no_user", rid)
        return
    # Positive: `now` is before the deadline, and the kill window ends after it.
    deadline_s = window - (now - request.created_at).total_seconds()
    slot = judges.JUDGE_SLOTS[judges.FIND]
    if slot.max_output_tokens is None or slot.floor_usd is None:  # the slot table declares both
        raise agent_error("unknown_model")
    call = StageCall(
        owner_id=request.owner_id,
        handle=handle,
        run_id=request.run_id,
        lesson_id=request.lesson_id,
        stage=judges.FIND,
        task=judges.FIND,
        attempt=1,
        system=judges.prompt_bytes(judges.FIND).decode("utf-8"),
        prompt=prompt,
        max_output_tokens=slot.max_output_tokens,
        floor_usd=slot.floor_usd,
        run_cap_usd=request.run_cap_usd,
        daily_budget_usd=request.daily_budget_usd,
        thinking_off=True,
        abort_on_retry=True,
        deadline_s=deadline_s,
        line=request.line,
    )
    try:
        async with deps.provider(engine) as provider:
            result = await provider.run(call)
        output = judges.parse_find_output(result.text, [card_id for card_id, _ in cards])
    except AgentError as error:
        log.info("judge_end request=%s code=%s", rid, error.code)
        return
    async with engine.begin() as write:
        if not await deps.guard.lock_active(write, request.pack_ids):
            log.info("judge_end request=%s code=pack_inactive", rid)
            return
        inserted = await judges.record(
            write, request.owner_id, input_sha256, judge, output, pack_ids=request.pack_ids, request_id=rid
        )
        if inserted:
            await deps.sink.copy(write, request, output)
    log.info("judge_end request=%s code=ok inserted=%s", rid, inserted)


def make_kind(deps: Deps) -> JobKind:
    return JobKind(
        name=KIND,
        queue=QUEUE,
        args=JudgeFindArgs,
        key=("request_id",),
        handler=functools.partial(judge_find, deps),
        max_attempts=1,
        timeout_s=LANE_TOKEN_TTL_S,
        key_retention_s=KEY_RETENTION_S,
    )


def _config() -> ReaderConfig:
    return load_config(Path(os.environ.get("READER_CONFIG", DEFAULT_CONFIG)))


@asynccontextmanager
async def _agent_provider(engine: AsyncEngine) -> AsyncIterator[Provider]:
    """The production provider: the lane's agent-proxy client (1 s issue timeout), closed after the call."""
    from reader.agent.provider import AgentProvider

    proxy = proxy_client()
    try:
        yield AgentProvider(engine, _config(), proxy)
    finally:
        await proxy.aclose()


def default_deps() -> Deps:
    """Production wiring (V.kl-deploy-judge-lane): SRV-EVENTS' `judge_requests` store serves the requests."""
    from reader.jobs.judge_requests import STORE

    return Deps(provider=_agent_provider, config=_config, requests=STORE)


JOB_KINDS: Final = (make_kind(default_deps()),)  # SRV-EVENTS KIND_MODULES lists this module


class _RevokeAll(Protocol):
    async def revoke_all(self) -> int | None: ...

    async def aclose(self) -> None: ...


async def on_worker_start(jobs: Any, *, proxy: _RevokeAll | None = None) -> None:
    """Revoke this client's stale tokens once at start; never raises (stale tokens expire within the TTL)."""
    client = proxy if proxy is not None else proxy_client()
    try:
        revoked = await client.revoke_all()
        log.info("lane_start revoked=%s", revoked)
    except Exception:  # isolated: a failed revoke-all only delays the quota by one TTL
        log.warning("lane_start revoke_all_failed")
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()
