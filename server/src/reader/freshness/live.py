"""`python -I -m reader.freshness.live`: FR-FRESH-14's server half, run in the worker container.

It runs **in-process** because R, the link set every stored URL must come from, exists only in the memory of
the process that runs the searches (FR-FRESH-7). For the owner's first imported material with an
owner-entered bibliography, it calls the route handlers of `reader.freshness.routes.ROUTES` directly, with
the owner as the signed-in user (the HTTP dispatcher, its session lookup and its CSRF gate are SRV-AUTH's and
are not exercised here):
  1. GET …/freshness, free: the estimate, which the POST must echo;
  2. POST …/freshness with that `high_usd`: the same pre-checks and insert as from the card; its enqueue is
     recorded instead of reaching the queue;
  3. the scan and web job handlers, in this process, with the real Agent SDK provider wrapped by a recorder
     that keeps every search run's R (memory only) and gate decision;
  4. GET …/freshness/{report_id}, free: the stored report, whose URLs are compared with R.
It prints one JSON object of ids, counts, flags and costs, and per search the gate's decision and the run's
summed uncached input tokens (`run_input_tokens`: `model_usage` input of the main loop's requests plus the
nested search request; input only, no cache writes or reads, so an indication of t, never t itself). The
cost cross-check compares each settle row's priced `usd` with the SDK's `sdk_usd` and holds only over at
least one such row (agent-proxy records no usage). Never a query, title, URL, bibliography field or model
text.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.requests import Request

from reader.agent import ledger
from reader.agent.pricing import crosscheck_ok
from reader.agent.provider import AgentProvider, HttpProxyClient, StageCall, StageResult
from reader.api.http import Ctx, Route
from reader.auth.policy import settings_from_config
from reader.auth.store import SessionUser
from reader.config import ReaderConfig, container_registry, load_config, resolve
from reader.db import DbTarget, make_engine
from reader.freshness import jobs, params, routes, store
from reader.freshness.links import collect
from reader.jobs.model import JobContext
from reader.serve.main import DB_PASSWORD_REF, Env


@dataclass
class Recorder:
    """Wraps the real provider: keeps every search run's tool results (R) and gate decisions in memory."""

    inner: Any
    searches: list[dict[str, Any]] = field(default_factory=list)
    urls: set[str] = field(default_factory=set)

    async def run(self, call: StageCall) -> StageResult:
        result: StageResult = await self.inner.run(call)
        if call.web_search is not None:
            gate = call.web_search.hook
            found = collect(result.tool_results, 1)
            self.urls.update(e.url for e in found.entries)
            decision = "allowed" if getattr(gate, "allowed", 0) else "denied"
            status = "ok" if found.entries else "empty"
            self.searches.append({"gate": decision, "run_input_tokens": result.usage.input, "status": status})
        return result


@dataclass
class Queue:
    """Stands in for the api's Jobs on the POST: records the kinds it would enqueue."""

    kinds: list[str] = field(default_factory=list)

    async def enqueue(self, kind: str, args: Any, schedule_in: int = 0) -> None:
        self.kinds.append(kind)


def db_target(env: Env) -> DbTarget:
    """The worker's database target from its container secret (public API only)."""
    password = resolve(DB_PASSWORD_REF, container_registry(env.secrets_root))
    return DbTarget(env.db_host, env.db_port, env.db_user, env.db_name, password)


def _route(method: str, suffix: str) -> Route:
    return next(r for r in routes.ROUTES if r.method == method and r.path.endswith(suffix))


@dataclass
class Caller:
    """Calls a declared route's handler in-process as the owner (no HTTP, no session, no CSRF gate)."""

    engine: AsyncEngine
    config: ReaderConfig
    user: SessionUser
    queue: Queue

    async def call(self, method: str, suffix: str, path_params: dict[str, str], body: Any = None) -> Any:
        payload = b"" if body is None else json.dumps(body).encode("utf-8")
        app = SimpleNamespace(state=SimpleNamespace(freshness_config=self.config, jobs=self.queue))
        scope = {
            "type": "http",
            "method": method,
            "path": "/",
            "query_string": b"",
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
            ],
            "path_params": path_params,
            "app": app,
        }

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": payload, "more_body": False}

        settings = settings_from_config(self.config)
        ctx = Ctx(Request(scope, receive), settings, self.engine, self.user)
        response = await _route(method, suffix).handler(ctx)
        return response.status_code, json.loads(bytes(response.body))


async def _owner(engine: AsyncEngine, handle: str) -> tuple[SessionUser, uuid.UUID]:
    async with engine.connect() as conn:
        user = (
            await conn.execute(
                text("SELECT id, handle, role, display_name FROM users WHERE handle = :h"), {"h": handle}
            )
        ).one()
        material_id = (
            await conn.execute(
                text(
                    "SELECT m.id FROM materials m JOIN ingests i ON i.material_id = m.id"
                    " WHERE m.owner_id = :o ORDER BY m.created_at LIMIT 1"
                ),
                {"o": user.id},
            )
        ).scalar_one()
        bib = await store.bibliography(conn, user.id, material_id)
    if bib is None or bib["source"] != "owner":
        raise SystemExit("the live check needs an owner-entered bibliography (PUT …/bibliography)")
    signed_in = SessionUser(
        user.id, user.handle, user.role, user.display_name, False, False, datetime.now(UTC), 0.0
    )
    return signed_in, material_id


async def _run() -> dict[str, Any]:
    env = Env.read(os.environ)
    config = load_config(Path(env.config))
    owner = next(u for u in config.users if u.role == "owner")
    engine = make_engine(db_target(env))
    proxy = HttpProxyClient()
    recorder = Recorder(AgentProvider(engine, config, proxy))
    queued: list[str] = []

    class WebQueue:
        async def enqueue(self, kind: str, args: Any) -> None:
            queued.append(kind)

    @asynccontextmanager
    async def provider(_: AsyncEngine) -> AsyncIterator[Any]:
        yield recorder

    deps = jobs.Deps(provider=provider, config=lambda: config, jobs=WebQueue)
    try:
        user, material_id = await _owner(engine, owner.handle)
        caller = Caller(engine, config, user, Queue())
        material = {"material_id": str(material_id)}
        status, before = await caller.call("GET", "/freshness", material)
        assert status == 200, before  # noqa: S101
        high_usd = before["estimate"]["high_usd"]
        status, started = await caller.call("POST", "/freshness", material, {"high_usd": high_usd})
        assert status == 202, started  # noqa: S101
        assert caller.queue.kinds == [params.KIND_SCAN]  # noqa: S101
        report_id = uuid.UUID(started["report_id"])
        ctx = JobContext(job_id=0, key="live", attempts=0)
        args = jobs.ReportArgs(report_id=report_id)
        async with engine.connect() as conn:
            await jobs.run_scan(deps, args, conn, ctx)
            if params.KIND_WEB in queued:
                await jobs.run_web(deps, args, conn, ctx)
        ids = {**material, "report_id": str(report_id)}
        status, report = await caller.call("GET", "/freshness/{report_id}", ids)
        assert status == 200, report  # noqa: S101
        stored = [c["url"] for r in report["reasons"] or [] for c in r["citations"]]
        stored += [c["url"] for s in report["claim_status"] or [] for c in s["citations"]]
        stored += [r["url"] for r in report["recommendations"] or []]
        settles = await _settles(engine, report_id)
        compared = [(Decimal(s[0]), Decimal(s[1])) for s in settles if s[0] is not None and s[1] is not None]
        return {
            "report": report_id.hex[:8],
            "estimate_high_usd": high_usd,
            "status": report["status"],
            "error": report["error"],
            "verdict": report["verdict"],
            "searches_with_results": sum(1 for s in recorder.searches if s["status"] == "ok"),
            "stored_urls": len(stored),
            "stored_urls_in_r": all(u in recorder.urls for u in stored),
            "usd": float(await ledger.spent(engine, run_id=report_id)),
            "ledger_rows_with_web_search_requests": sum(1 for s in settles if s[2]),
            "crosscheck_rows": len(compared),
            "crosscheck_ok": bool(compared) and all(crosscheck_ok(sdk, usd) for sdk, usd in compared),
            "searches": recorder.searches,
        }
    finally:
        with contextlib.suppress(Exception):
            await proxy.aclose()
        await engine.dispose()


async def _settles(engine: AsyncEngine, report_id: Any) -> list[Any]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT sdk_usd, usd, web_search_requests FROM agent_spend"
                " WHERE run_id = :r AND kind = 'settle'"
            ),
            {"r": report_id},
        )
        return list(rows)


def main() -> int:
    sys.stdout.write(json.dumps(asyncio.run(_run())) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
