"""The two job kinds (FR-FRESH-12; SRV-EVENTS KIND_MODULES), keyed by report id.

`freshness.scan` runs stage (a) (or reuses an earlier scan, FR-FRESH-3) and, when stage (b) may run
(FR-FRESH-1), enqueues `freshness.web`. `freshness.web` runs every planned search call in plan order and then
the report call, in one job: R and the summaries live only in its memory (FR-FRESH-7). A call that fails
transiently is retried once, only when it settled at $0 and the job's time covers the call's budget; the
report call may have one paid retry. A second job attempt restarts the stage from its start.

The handlers write every final status themselves and never raise for a call's outcome; report writes go
through their own short transactions, so the routes see progress at once. Each stage end logs one line
(FR-FRESH-13): report id prefix, stage, index, status, code, searches, denials, links dropped, cost and
duration; never a query, URL, title, bibliography field, span text, summary, prompt or model output.
"""

from __future__ import annotations

import functools
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from reader.agent import ledger
from reader.agent.errors import AgentError, BudgetRefused, TransientAgentError
from reader.agent.provider import Provider, StageCall, WebSearchAllowance, WebSearchCall
from reader.config import ReaderConfig, load_config
from reader.freshness import check, links, params, prompts, queries, scan, store, verdict
from reader.freshness.bibliography import sha256
from reader.freshness.errors import FreshError
from reader.freshness.estimate import report_reserve, scan_reserve, search_reserve
from reader.freshness.gate import Gate
from reader.freshness.sample import canonical_sha256
from reader.jobs.model import JobContext, JobKind

log = logging.getLogger("reader.freshness")
DEFAULT_CONFIG: Final = "/etc/reader/reader.yaml"
ATTEMPT_STRIDE: Final = 100  # attempt number = job attempt * 100 + call index: unique per (run, stage)


class ReportArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_id: uuid.UUID


def _config() -> ReaderConfig:
    return load_config(Path(os.environ.get("READER_CONFIG", DEFAULT_CONFIG)))


@asynccontextmanager
async def _agent_provider(engine: AsyncEngine) -> AsyncIterator[Provider]:
    from reader.agent.provider import AgentProvider, HttpProxyClient

    proxy = HttpProxyClient()
    try:
        yield AgentProvider(engine, _config(), proxy)
    finally:
        await proxy.aclose()


def _running_jobs() -> Any:
    from reader.jobs import running

    return running()


@dataclass(frozen=True)
class Deps:
    """What the handlers reach outside their connection; tests pass fakes."""

    provider: Callable[[AsyncEngine], AbstractAsyncContextManager[Provider]]
    config: Callable[[], ReaderConfig]
    jobs: Callable[[], Any]
    today: Callable[[], date] = check.today_utc
    clock: Callable[[], float] = time.monotonic


def default_deps() -> Deps:
    return Deps(provider=_agent_provider, config=_config, jobs=_running_jobs)


@dataclass
class _Run:
    """One job attempt: the report, its time budget and the per-call attempt numbers."""

    engine: AsyncEngine
    report: store.Report
    handle: str
    deps: Deps
    job_attempt: int
    timeout_s: float
    started: float
    calls: int = 0
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def id(self) -> uuid.UUID:
        return self.report.id

    @property
    def owner_id(self) -> uuid.UUID:
        owner: uuid.UUID = self.report["owner_id"]
        return owner

    def next_attempt(self) -> int:
        self.calls += 1
        return self.job_attempt * ATTEMPT_STRIDE + self.calls

    def time_left(self, budget_s: float) -> bool:
        return self.timeout_s - (self.deps.clock() - self.started) >= budget_s

    def count(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def stage_call(self, attempt: int, **fields: Any) -> StageCall:
        return StageCall(
            owner_id=self.owner_id,
            handle=self.handle,
            run_id=self.id,
            lesson_id=None,
            attempt=attempt,
            run_cap_usd=params.RUN_CAP_USD,
            daily_budget_usd=params.DAILY_CAP_USD,
            thinking_off=True,
            **fields,
        )


async def _with_retry[T](
    run: _Run, stage: str, call: Callable[[int], Awaitable[T]], *, budget_s: float, paid_retry: bool
) -> T:
    """FR-FRESH-12: one retry of a transient failure, after a $0 settle (or always, for `paid_retry`), and
    only when the job's remaining time covers the call's budget."""
    attempt = run.next_attempt()
    try:
        return await call(attempt)
    except BudgetRefused:
        raise
    except TransientAgentError as error:
        if not run.time_left(budget_s):
            raise
        if not paid_retry:
            settled = await store.settled_of(run.engine, run.id, stage, attempt)
            if settled is None or settled != 0:
                raise
        log.info("freshness_retry report=%s stage=%s code=%s", run.id.hex[:8], stage, error.code)
        return await call(run.next_attempt())


def _merge(into: dict[str, int], more: Any) -> dict[str, int]:
    for key, value in dict(more or {}).items():
        into[key] = into.get(key, 0) + int(value)
    return into


async def _log_end(run: _Run, stage: str, index: int | None, status: str, code: str | None) -> None:
    spent = await ledger.spent(run.engine, run_id=run.id)
    log.info(
        "freshness_stage report=%s stage=%s index=%s status=%s code=%s searches=%s denials=%s"
        " links_dropped=%s cost=%s seconds=%.1f",
        run.id.hex[:8], stage, index, status, code, run.counts.get("searches", 0),
        run.counts.get("denials", 0), run.counts.get("links_dropped", 0), spent,
        run.deps.clock() - run.started,
    )


async def _final(run: _Run, stage: str, status: str, code: str | None, fields: dict[str, Any]) -> None:
    cost = await ledger.spent(run.engine, run_id=run.id)
    await store.finish(run.engine, run.id, status, error=code, cost_usd=cost, **fields)
    await _log_end(run, stage, None, status, code)


async def _start(
    deps: Deps, args: ReportArgs, conn: AsyncConnection, ctx: JobContext, timeout_s: float
) -> _Run | None:
    engine = conn.engine
    async with engine.connect() as c:
        report = await store.load(c, args.report_id)
        if report is None or report.status != "running":
            log.info("freshness_skip report=%s", args.report_id.hex[:8])
            return None
        handle = await store.handle_of(c, report["owner_id"])
    return _Run(engine, report, handle, deps, ctx.attempts, timeout_s, deps.clock())


async def _prepared(run: _Run, stage: str) -> check.Prepared | None:
    try:
        config = run.deps.config()
        async with run.engine.connect() as c:
            material = await store.material(c, run.owner_id, run.report["material_id"])
            if material is None or material.ingest_id != run.report["ingest_id"]:
                raise FreshError("not_imported")
            return await check.prepare(c, config, run.owner_id, material)
    except FreshError as error:
        await _final(run, stage, "failed", error.code, {})
    except Exception:  # an unreadable reader.yaml
        await _final(run, stage, "failed", "config_unavailable", {})
    return None


# ---- freshness.scan --------------------------------------------------------------------------------------


async def run_scan(deps: Deps, args: ReportArgs, conn: AsyncConnection, ctx: JobContext) -> None:
    run = await _start(deps, args, conn, ctx, params.SCAN_JOB_TIMEOUT_S)
    if run is None:
        return
    prep = await _prepared(run, "scan")
    if prep is None:
        return
    prompt = prompts.load("scan")
    records: dict[str, Any] = {"models": dict(prep.models), "prompt_versions": {"scan": prompt.record()}}
    if run.report["claims"] is None:
        async with run.engine.connect() as c:
            reused = await store.reusable_scan(
                c, run.owner_id, prep.material.id, run.report["scan_inputs_sha256"], run.id
            )
        if reused is not None:
            scanned_bib = reused["scan_bibliography"]
            await store.update(
                run.engine, run.id, claims=reused["claims"], scan_bibliography=scanned_bib,
                scan_reused_from=reused.id, **records,
            )
        else:
            outcome = await _scan_call(run, prep, prompt)
            if outcome is None:
                return
            scanned_bib = outcome.bibliography
            await store.update(
                run.engine, run.id, claims=outcome.claims, scan_bibliography=scanned_bib,
                dropped=dict(outcome.dropped) or None, **records,
            )
        if isinstance(scanned_bib, dict):  # FR-FRESH-1: copied only where no owner entry exists
            async with run.engine.begin() as c:
                await store.save_bibliography(c, run.owner_id, prep.material.id, scanned_bib, "scan")
    if run.report["bibliography"] is None:  # FR-FRESH-11: no owner entry and no confirmation: scan only
        await _final(run, "scan", "done", None, records)
        return
    try:
        await deps.jobs().enqueue(params.KIND_WEB, {"report_id": str(run.id)})
    except Exception as failure:
        log.warning("freshness_enqueue_failed report=%s error=%s", run.id.hex[:8], type(failure).__name__)
        await _final(run, "scan", "failed", "job_failed", {})
        return
    await _log_end(run, "scan", None, "running", None)


async def _scan_call(run: _Run, prep: check.Prepared, prompt: prompts.Prompt) -> scan.ScanResult | None:
    system, user = scan.call_texts(prep.sample, prompt)
    _, aliases = scan.render(prep.sample)
    model = prep.models[params.TASK_SCAN]
    if await check.daily_refused(run.engine, run.owner_id, scan_reserve(model, len(system) + len(user))):
        await _final(run, "scan", "budget_refused", "daily_budget_exhausted", {})
        return None
    today = run.deps.today()

    async def call(attempt: int) -> scan.ScanResult:
        async with run.deps.provider(run.engine) as provider:
            result = await provider.run(
                run.stage_call(
                    attempt, stage="scan", task=params.TASK_SCAN, system=system, prompt=user,
                    max_output_tokens=params.SCAN_OUTPUT_TOKENS, floor_usd=Decimal(0),
                )
            )
        return scan.parse(result.text, aliases, prep.material.sha256, today)

    budget = params.SCAN_JOB_TIMEOUT_S / 2
    try:
        return await _with_retry(run, "scan", call, budget_s=budget, paid_retry=False)
    except BudgetRefused as refused:
        await _final(run, "scan", "budget_refused", refused.code, {})
    except AgentError as error:
        await _final(run, "scan", "failed", error.code, {})
    return None


# ---- freshness.web ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchOutcome:
    status: str  # ok | empty | denied | failed | refused (FR-FRESH-7)
    found: links.Found | None = None
    requests: int = 0
    denials: int = 0
    code: str | None = None


async def run_web(deps: Deps, args: ReportArgs, conn: AsyncConnection, ctx: JobContext) -> None:
    run = await _start(deps, args, conn, ctx, params.WEB_JOB_TIMEOUT_S)
    if run is None:
        return
    report = run.report
    if report["claims"] is None or not isinstance(report["bibliography"], dict):
        await _final(run, "search", "failed", "job_failed", {})
        return
    prep = await _prepared(run, "search")
    if prep is None:
        return
    bib = check.fields_of(report["bibliography"])
    claims: list[dict[str, Any]] = list(report["claims"])
    texts = prep.sample.non_front_texts
    plan = queries.plan(bib, prep.topics, claims, prep.language, deps.today(), texts)
    searches = [
        {"query": q.text, "template": q.template, "status": "pending", "result_count": 0}
        for q in plan.queries
    ]
    # report["dropped"] is still the scan's: this job writes `dropped` only with its final status, so a second
    # attempt (FR-FRESH-12) counts nothing twice.
    dropped = _merge(_merge({}, report["dropped"]), plan.dropped)
    await store.update(run.engine, run.id, searches=searches)
    search_prompt = prompts.load("search")
    report_prompt = prompts.load("report")
    versions = {"scan": prompts.load("scan").record(), "search": search_prompt.record()}
    records: dict[str, Any] = {
        "models": dict(prep.models),
        "prompt_versions": {**versions, "report": report_prompt.record()},
    }
    entries: dict[str, links.Entry] = {}
    views: list[verdict.SearchView] = []
    web_requests = 0
    for index, query in enumerate(plan.queries):
        out = await _search(run, prep, search_prompt, query.text, len(entries) + 1)
        web_requests += out.requests
        run.count("searches")
        run.count("denials", out.denials)
        if out.denials:
            _merge(dropped, {"query_denied": out.denials})
        if out.found is not None:
            _merge(dropped, out.found.dropped)
            run.count("links_dropped", sum(out.found.dropped.values()))
            entries.update({e.id: e for e in out.found.entries})
            views.append(verdict.SearchView(query.text, out.found.entries, out.found.summary))
        count = len(out.found.entries) if out.found is not None else 0
        searches[index] = {**searches[index], "status": out.status, "result_count": count}
        stored = {"searches": searches, "dropped": dropped or None, "web_search_requests": web_requests}
        if out.status == "refused":
            await _final(run, "search", "budget_refused", out.code, {**stored, **records})
            return
        await store.update(run.engine, run.id, searches=searches)
        await _log_end(run, "search", index, out.status, out.code)
    stored = {"searches": searches, "web_search_requests": web_requests, **records}
    if not entries:
        await _final(run, "search", "failed", "no_results", {**stored, "dropped": dropped or None})
        return
    await _report_stage(run, prep, report_prompt, bib, claims, views, entries, dropped, stored)


def search_prompt_text(query: str) -> str:
    """The search run's user prompt: the planned query on one line."""
    return f"Query: {query}"


def search_call(
    stage_call: Callable[..., StageCall], attempt: int, prompt: prompts.Prompt, query: str, gate: Gate
) -> StageCall:
    """FR-FRESH-7's StageCall for one planned query under its gate (FR-FRESH-6). The INV-FRESH-1 [agent] test
    builds its calls through this function too, so it runs the same options as the job."""
    allowance = WebSearchAllowance(
        searches_max=1, searches_per_request_max=1, requests_max=params.SEARCH_REQUESTS_MAX
    )
    return stage_call(
        attempt=attempt, stage="search", task=params.TASK_WEB, system=prompt.text,
        prompt=search_prompt_text(query), max_output_tokens=params.SEARCH_OUTPUT_TOKENS,
        floor_usd=params.SEARCH_RESERVE_FLOOR_USD, abort_on_retry=True,
        deadline_s=float(params.SEARCH_DEADLINE_S),
        web_search=WebSearchCall(allowance=allowance, hook=gate, max_turns=params.SEARCH_MAX_TURNS),
    )


async def _search(
    run: _Run, prep: check.Prepared, prompt: prompts.Prompt, query: str, first_k: int
) -> SearchOutcome:
    """One search run under its gate; R entries only from the structured tool results."""
    if not run.time_left(params.SEARCH_CALL_BUDGET_S):
        return SearchOutcome("failed", code="deadline_exceeded")
    model = prep.models[params.TASK_WEB]
    reserve = search_reserve(model, len(prompt.text) + len(search_prompt_text(query)))
    if await check.daily_refused(run.engine, run.owner_id, reserve):
        return SearchOutcome("refused", code="daily_budget_exhausted")
    gates: list[Gate] = []

    async def call(attempt: int) -> Any:
        gate = Gate(query)  # a fresh gate per attempt: one allowed search per run
        gates.append(gate)
        async with run.deps.provider(run.engine) as provider:
            return await provider.run(search_call(run.stage_call, attempt, prompt, query, gate))

    budget = float(params.SEARCH_CALL_BUDGET_S)
    try:
        result = await _with_retry(run, "search", call, budget_s=budget, paid_retry=False)
    except BudgetRefused as refused:
        return SearchOutcome("refused", denials=sum(g.denied for g in gates), code=refused.code)
    except AgentError as error:
        return SearchOutcome("failed", denials=sum(g.denied for g in gates), code=error.code)
    denials = sum(g.denied for g in gates)
    found = links.collect(result.tool_results, first_k)
    requests = int(result.usage.web_search_requests or 0)
    if found.entries:
        return SearchOutcome("ok", found, requests, denials)
    allowed = sum(g.allowed for g in gates)
    return SearchOutcome("denied" if allowed == 0 and denials else "empty", found, requests, denials)


async def _report_stage(
    run: _Run,
    prep: check.Prepared,
    prompt: prompts.Prompt,
    bib: dict[str, Any],
    claims: list[dict[str, Any]],
    views: list[verdict.SearchView],
    entries: dict[str, links.Entry],
    dropped: dict[str, int],
    stored: dict[str, Any],
) -> None:
    today = run.deps.today()
    topics = prep.topics
    user = verdict.render(bib, topics, claims, views, today)
    model = prep.models[params.TASK_REPORT]
    reserve = report_reserve(model, len(prompt.text) + len(user))
    if await check.daily_refused(run.engine, run.owner_id, reserve):
        fields = {**stored, "dropped": dropped or None}
        await _final(run, "report", "budget_refused", "daily_budget_exhausted", fields)
        return

    async def call(attempt: int) -> dict[str, Any]:
        async with run.deps.provider(run.engine) as provider:
            result = await provider.run(
                run.stage_call(
                    attempt, stage="report", task=params.TASK_REPORT, system=prompt.text, prompt=user,
                    max_output_tokens=params.REPORT_OUTPUT_TOKENS, floor_usd=Decimal(0),
                )
            )
        return verdict.parse(result.text)

    budget = float(params.REPORT_CALL_BUDGET_S)
    try:
        parsed = await _with_retry(run, "report", call, budget_s=budget, paid_retry=True)
    except BudgetRefused as refused:
        await _final(run, "report", "budget_refused", refused.code, {**stored, "dropped": dropped or None})
        return
    except AgentError as error:
        await _final(run, "report", "failed", error.code, {**stored, "dropped": dropped or None})
        return
    resolved = verdict.resolve(parsed, entries, {c["id"] for c in claims}, {t.id for t in topics}, today)
    _merge(dropped, resolved.dropped)
    versions = stored["prompt_versions"]
    inputs = canonical_sha256(
        {
            "scan_inputs_sha256": run.report["scan_inputs_sha256"],
            "bibliography_sha256": sha256(bib),
            "topics": [[t.id, t.title] for t in topics],
            "queries": [s["query"] for s in stored["searches"]],
            "prompts": {"search": versions["search"], "report": versions["report"]},
            "models": stored["models"],
        }
    )
    fields = {
        **stored,
        "verdict": resolved.verdict,
        "reasons": resolved.reasons,
        "claim_status": resolved.claim_status,
        "recommendations": resolved.recommendations,
        "dropped": dropped or None,
        "inputs_sha256": inputs,
    }
    await _final(run, "report", "done", None, fields)


def make_kinds(deps: Deps) -> tuple[JobKind, JobKind]:
    def kind(name: str, handler: Any, timeout_s: int) -> JobKind:
        return JobKind(
            name=name,
            queue="freshness",
            args=ReportArgs,
            key=("report_id",),
            handler=functools.partial(handler, deps),
            max_attempts=params.MAX_ATTEMPTS,
            transient=(),
            timeout_s=float(timeout_s),
        )

    return (
        kind(params.KIND_SCAN, run_scan, params.SCAN_JOB_TIMEOUT_S),
        kind(params.KIND_WEB, run_web, params.WEB_JOB_TIMEOUT_S),
    )


JOB_KINDS: Final = make_kinds(default_deps())

__all__ = [
    "JOB_KINDS", "Deps", "ReportArgs", "SearchOutcome", "make_kinds", "run_scan", "run_web", "search_call",
    "search_prompt_text",
]
