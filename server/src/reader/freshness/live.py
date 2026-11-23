"""`python -I -m reader.freshness.live`: FR-FRESH-14's server half, run in the worker container.

For the owner's first imported material with an owner-entered bibliography, it starts one check the way
the POST route does (the same estimate, sample and insert), then runs the scan and web handlers in this
process with the real Agent SDK provider wrapped by a recorder, so R (memory only) can be compared with the
stored URLs. It prints one JSON object of ids, counts, flags and costs, and per search the gate's decision
and the run's summed input tokens (t plus the run's own prompts, an upper bound of t). Never a query,
title, URL, bibliography field or model text.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from reader.agent import ledger
from reader.agent.pricing import crosscheck_ok
from reader.agent.provider import AgentProvider, HttpProxyClient, StageCall, StageResult
from reader.config import load_config
from reader.db import make_engine
from reader.freshness import check, jobs, params, store
from reader.freshness.links import collect
from reader.jobs.model import JobContext
from reader.serve.main import Env, _target


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
            tokens = result.usage.input
            self.searches.append({"gate": decision, "nested_input_tokens": tokens, "status": status})
        return result


async def _start(engine: AsyncEngine, config: Any, handle: str) -> tuple[Any, Any]:
    async with engine.connect() as conn:
        found = await conn.execute(text("SELECT id FROM users WHERE handle = :h"), {"h": handle})
        owner_id = found.scalar_one()
        material_id = (
            await conn.execute(
                text(
                    "SELECT m.id FROM materials m JOIN ingests i ON i.material_id = m.id"
                    " WHERE m.owner_id = :o ORDER BY m.created_at LIMIT 1"
                ),
                {"o": owner_id},
            )
        ).scalar_one()
        material = await store.material(conn, owner_id, material_id)
        assert material is not None  # noqa: S101 (joined above)
        prep = await check.prepare(conn, config, owner_id, material)
        bib = await store.bibliography(conn, owner_id, material_id)
    if bib is None or bib["source"] != "owner":
        raise SystemExit("the live check needs an owner-entered bibliography (PUT …/bibliography)")
    report_id = await store.insert(
        engine, owner_id, material_id, prep.ingest_id, prep.scan_inputs_sha256, bib,
        prep.sample.stored(material.sha256),
    )
    return owner_id, report_id


async def _run() -> dict[str, Any]:
    env = Env.read(os.environ)
    config = load_config(Path(env.config))
    owner = next(u for u in config.users if u.role == "owner")
    engine = make_engine(_target(env))
    proxy = HttpProxyClient()
    recorder = Recorder(AgentProvider(engine, config, proxy))
    queued: list[str] = []

    class Queue:
        async def enqueue(self, kind: str, args: Any) -> None:
            queued.append(kind)

    @asynccontextmanager
    async def provider(_: AsyncEngine) -> Any:
        yield recorder

    deps = jobs.Deps(provider=provider, config=lambda: config, jobs=Queue)
    try:
        _, report_id = await _start(engine, config, owner.handle)
        ctx = JobContext(job_id=0, key="live", attempts=0)
        args = jobs.ReportArgs(report_id=report_id)
        async with engine.connect() as conn:
            await jobs.run_scan(deps, args, conn, ctx)
            if params.KIND_WEB in queued:
                await jobs.run_web(deps, args, conn, ctx)
            report = await store.load(conn, report_id)
        assert report is not None  # noqa: S101
        stored = [c["url"] for r in report["reasons"] or [] for c in r["citations"]]
        stored += [c["url"] for s in report["claim_status"] or [] for c in s["citations"]]
        stored += [r["url"] for r in report["recommendations"] or []]
        settles = await _settles(engine, report_id)
        return {
            "report": report_id.hex[:8],
            "status": report.status,
            "error": report["error"],
            "verdict": report["verdict"],
            "searches_with_results": sum(1 for s in report["searches"] or [] if s["result_count"]),
            "stored_urls": len(stored),
            "stored_urls_in_r": all(u in recorder.urls for u in stored),
            "usd": float(await ledger.spent(engine, run_id=report_id)),
            "ledger_rows_with_web_search_requests": sum(1 for s in settles if s[2]),
            "crosscheck_ok": all(
                crosscheck_ok(Decimal(s[0]), Decimal(s[1])) for s in settles if s[0] and s[1]
            ),
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
