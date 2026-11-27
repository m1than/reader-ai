"""SRV-SEARCH FR-SEARCH-28: the `judge.find` waiter (ADR-0039 B6, B7b), find's production `Judge` port.

One request at most per `find`, never retried: the recorded output first ($0, no row, no job); else the
process's listener registers the request id, one committed transaction writes the `judge_requests` row (its
`created_at` and `deadline` from the database's `now()`), the `judge.find` job is enqueued with args
`{request_id, input_sha256}` only, and the waiter waits at most `judge.timeout_s` for the lane's
notification `<request_id>:<code>` (a bare id reads as `ok` until SRV-AGENT sends codes). The query and card
text live only in the row; nothing here logs them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from reader.agent import judges
from reader.agent.lane import KIND, NOTIFY_CHANNEL, _config
from reader.agent.pricing import resolve_model
from reader.config import ReaderConfig
from reader.jobs.judge_requests import STORE, NewJudgeRequest, Store
from reader.search.find import JudgeCall, JudgeOutcome
from reader.search.policy import Policy

CODES: Final = frozenset(
    {"ok", "recorded", "output_invalid", "budget", "saturated", "quota_refused", "expired", "error"}
)
APPLIED: Final = frozenset({"ok", "recorded"})
PASSED_ON: Final = frozenset({"budget", "saturated", "quota_refused"})  # degrade with that judge_skip
CONTEXT_KEYS: Final = ("line", "run_id", "run_cap_usd", "lesson_id", "daily_budget_usd")

log = logging.getLogger("reader.search")


def parse(payload: str) -> tuple[str, str]:
    """(request id, code): a code outside the closed set is `error`; a payload without `:` is `ok`."""
    request_id, sep, code = payload.partition(":")
    if not sep:
        return request_id, "ok"
    return request_id, code if code in CODES else "error"


class Listener:
    """One connection holding `LISTEN judge_done`, dispatching each payload to the request waiting for it."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self._conn: AsyncConnection | None = None
        self._driver: Any = None
        self._waiting: dict[str, asyncio.Future[str]] = {}
        self._lock = asyncio.Lock()

    @property
    def alive(self) -> bool:
        return self._driver is not None and not self._driver.is_closed()

    async def start(self) -> None:
        conn = await self.engine.connect()
        try:
            raw = await conn.get_raw_connection()
            driver: Any = raw.driver_connection
            await driver.add_listener(NOTIFY_CHANNEL, self._notified)
            driver.add_termination_listener(self._lost)
        except BaseException:
            with contextlib.suppress(Exception):
                await conn.close()
            raise
        self._conn, self._driver = conn, driver

    async def ensure(self) -> bool:
        """Started (or restarted after a loss) before a request registers; False when that fails."""
        async with self._lock:
            if not self.alive:
                await self._drop()
                try:
                    await self.start()
                except Exception:
                    log.warning("judge_wait listener_down")
                    return False
        return self.alive

    def register(self, request_id: str) -> asyncio.Future[str]:
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._waiting[request_id] = future
        return future

    def discard(self, request_id: str) -> None:
        self._waiting.pop(request_id, None)

    def _notified(self, _conn: Any, _pid: int, _channel: str, payload: str) -> None:
        request_id, code = parse(payload)
        future = self._waiting.get(request_id)
        if future is not None and not future.done():
            future.set_result(code)

    def _lost(self, _conn: Any) -> None:
        """The connection ended: requests waiting on it degrade `error`; the next request restarts it."""
        self._driver = None
        for future in self._waiting.values():
            if not future.done():
                future.set_result("error")

    async def _drop(self) -> None:
        conn, self._conn, self._driver = self._conn, None, None
        if conn is not None:
            with contextlib.suppress(Exception):
                await conn.invalidate()
            with contextlib.suppress(Exception):
                await conn.close()

    async def close(self) -> None:
        async with self._lock:
            await self._drop()


_LISTENERS: dict[Any, Listener] = {}  # one per engine; a process has one engine


def listener(engine: AsyncEngine) -> Listener:
    found = _LISTENERS.get(engine.sync_engine)
    if found is None:
        found = _LISTENERS[engine.sync_engine] = Listener(engine)
    return found


async def stop(engine: AsyncEngine) -> None:
    found = _LISTENERS.pop(engine.sync_engine, None)
    if found is not None:
        await found.close()


def _skip(code: str) -> JudgeOutcome:
    return JudgeOutcome(kept=None, source=None, skip=code)


def _applied(output: Mapping[str, Any], source: str) -> JudgeOutcome:
    return JudgeOutcome(kept=tuple(str(c) for c in output.get("keep", ())), source=source, skip=None)


@dataclass
class Waiter:
    engine: AsyncEngine
    enqueue: Callable[[str, Mapping[str, Any]], Awaitable[Any]]  # SRV-EVENTS Jobs.enqueue
    policy: Policy
    config: Callable[[], ReaderConfig] = _config
    store: Store = STORE
    daily_budget_usd: Decimal | None = None  # the owner channel's (deviation: none until SRV-MCP's cap)
    listener: Listener | None = None  # None: the process's listener for `engine`

    async def __call__(self, call: JudgeCall) -> JudgeOutcome:
        try:
            return await self._judge(call)
        except Exception:  # any database or driver error: one request, no retry
            log.warning("judge_wait error")
            return _skip("error")

    async def _recorded(self, owner: uuid.UUID, input_sha256: str, judge: str) -> dict[str, Any] | None:
        async with self.engine.connect() as conn:
            return await judges.recorded(conn, owner, input_sha256, judge)

    def _context(self, call: JudgeCall) -> dict[str, Any] | None:
        given = call.judge_ctx
        if given is None:
            if call.channel != "owner":
                return None
            return {"line": "mcp", "run_id": uuid.uuid4(),
                    "run_cap_usd": Decimal(str(self.policy.data["owner_judge_run_cap_usd"])),
                    "lesson_id": None, "daily_budget_usd": self.daily_budget_usd}
        if not set(CONTEXT_KEYS) <= set(given):
            return None
        return {key: given[key] for key in CONTEXT_KEYS}

    async def _judge(self, call: JudgeCall) -> JudgeOutcome:
        try:
            task = self.config().tasks.get(judges.FIND)
        except Exception:  # an unreadable config: the slot is missing
            task = None
        if task is None:
            return _skip("error")
        judge = judges.find_judge(resolve_model(task.model))
        owner = call.caller.user_id
        input_sha256 = judges.find_input_sha256(owner, call.query, call.cards)
        output = await self._recorded(owner, input_sha256, judge)
        if output is not None:
            return _applied(output, "recorded")
        if not call.caller.provider:
            return _skip("no_provider")
        context = self._context(call)
        if context is None:
            return _skip("error")
        found = self.listener or listener(self.engine)
        if not await found.ensure():
            return _skip("error")
        request_id = uuid.uuid4()
        done = found.register(str(request_id))
        try:
            code = await self._request(call, request_id, input_sha256, context, done)
        finally:
            found.discard(str(request_id))
        if code not in APPLIED:
            return _skip(code if code in PASSED_ON or code == "timeout" else "error")
        output = await self._recorded(owner, input_sha256, judge)
        return _applied(output, "live") if output is not None else _skip("error")

    async def _request(self, call: JudgeCall, request_id: uuid.UUID, input_sha256: str,
                       context: Mapping[str, Any], done: asyncio.Future[str]) -> str:
        timeout_s = float(self.policy.data["judge"]["timeout_s"])
        started = time.monotonic()
        async with self.engine.begin() as conn:
            deadline = (await conn.execute(text("SELECT now() + make_interval(secs => :t)"),
                                           {"t": timeout_s})).scalar_one()
            await self.store.put(conn, NewJudgeRequest(
                request_id=request_id, owner_id=call.caller.user_id, input_sha256=input_sha256,
                deadline=deadline, query=call.query, cards=tuple(call.cards), pack_ids=tuple(call.pack_ids),
                **context,
            ))
        try:
            queued = await self.enqueue(KIND, {"request_id": str(request_id), "input_sha256": input_sha256})
            enqueued = getattr(queued, "kind", None) == "queued"
        except Exception:
            enqueued = False
        if not enqueued:
            async with self.engine.begin() as conn:
                await self.store.delete(conn, request_id)
            log.warning("judge_wait enqueue_failed")
            return "error"
        try:
            async with asyncio.timeout(max(0.0, timeout_s - (time.monotonic() - started))):
                return await done
        except TimeoutError:
            return "timeout"
