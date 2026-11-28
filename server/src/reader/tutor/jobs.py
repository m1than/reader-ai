"""The `tutor.answer` job (SRV-TUTOR FR-TUTOR-3..9, -12): retrieve, one model call, check, store.

The handler never leaves a row `queued`: every permanent outcome, every budget refusal and a transient error
on the last attempt end the row and return; only a transient error before the last attempt is raised again (as
`TutorTransient`) for SRV-EVENTS to retry. Its one write to `tutor_questions` is its last statement
(SRV-EVENTS FR-EVENTS-4). Logs carry ids, lengths, codes, the model, spend and timings, never a text.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from reader import blobs
from reader.agent import ledger
from reader.agent.errors import AgentError
from reader.agent.pricing import resolve_model
from reader.agent.provider import Provider, StageCall
from reader.config import ReaderConfig, load_config
from reader.ingest import locator
from reader.jobs.model import JobContext, JobKind, idempotency_key
from reader.lessons.access import can_open
from reader.obs import feedback, prompts
from reader.obs.spans import annotate_job, child_span, session_marker
from reader.search import find as find_mod
from reader.search import policy as policy_mod
from reader.search.corpus import StoreCorpus
from reader.search.find import Caller, Judge
from reader.search.find import Deps as FindDeps
from reader.search.policy import Policy
from reader.tutor import params, store
from reader.tutor.answer import (
    LOCATOR_PREFIX,
    Span,
    build_prompt,
    check,
    citations,
    floor_usd,
    parse,
    system_prompt,
)
from reader.tutor.errors import TRANSIENT, TutorError, TutorTransient, outcome

log = logging.getLogger("reader.tutor")
DEFAULT_CONFIG: Final = "/etc/reader/reader.yaml"


class AnswerArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: uuid.UUID


def _is_locator(span_id: str) -> bool:
    try:
        locator.parse(span_id)
    except locator.LocatorError:
        return False
    return True


def candidate_ids(cards: Sequence[Mapping[str, Any]]) -> list[str]:
    """FR-TUTOR-5, VEC-TUTOR-9: the L1 cards' ids in response order, then each L1 card's first `next` id that
    is a span locator, no repeats; the budget drops from the end, so next spans go before any primary. An id
    that is not a locator (a legacy `<sha>:<seq>` id) is dropped here, before loading, so it is never sent."""
    l1 = [card for card in cards if card.get("layer") == "L1"]
    ordered = [str(card["id"]) for card in l1]
    for card in l1:
        after = next((str(n) for n in card.get("next", ()) if str(n).startswith(LOCATOR_PREFIX)), None)
        if after is not None:
            ordered.append(after)
    return [i for i in dict.fromkeys(ordered) if _is_locator(i)]


class Find(Protocol):
    """SRV-SEARCH FR-SEARCH-22's `find` as the tutor calls it: no budget, no k, no judge context."""

    async def __call__(
        self, caller: Caller, channel: str, query: str, *, deps: FindDeps
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class Deps:
    """What the handler reaches outside its connection; tests pass fakes."""

    provider: Callable[[AsyncEngine], AbstractAsyncContextManager[Provider]]  # closed after the one call
    blobs_root: Path
    config: Callable[[], ReaderConfig]
    find: Find = find_mod.find
    policy: Callable[[], tuple[Policy, str]] = policy_mod.current  # one (policy, version) pair per question
    judge: Judge | None = None  # kl_open_points 2: not wired while the shipped gate is never_judge
    deadline_s: float = params.CALL_DEADLINE_S


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


def default_deps() -> Deps:
    return Deps(provider=_agent_provider, blobs_root=blobs.DEFAULT_ROOT, config=_config)


def job_key(question_id: uuid.UUID) -> str:
    return idempotency_key(JOB_KINDS[0], {"question_id": str(question_id)})


async def _retrieve(deps: Deps, conn: AsyncConnection, q: store.Question) -> tuple[list[Span], bool]:
    """FR-TUTOR-5: find on channel `tutor` over the course's material, then the candidates' stored spans."""
    if q.material_sha256 is None:
        raise TutorError("sources_unavailable")
    with child_span("tutor.retrieve") as retrieved:  # SRV-OBS FR-OBS-13: counts only
        try:
            policy, version = deps.policy()
            provider = any(u.handle == q.handle and u.providers for u in deps.config().users)
            caller = Caller(q.owner_id, (q.role,), provider)
            found = await deps.find(caller, params.TASK, q.question, deps=FindDeps(
                corpus=StoreCorpus(conn, policy, q.material_sha256), judge=deps.judge, policy=policy,
                policy_version=lambda: version))
        except (policy_mod.PolicyError, find_mod.FindError) as error:
            log.warning("tutor_find_error question=%s code=%s", q.id, error.code)
            raise TutorError("tutor_failed") from None
        cards = found["cards"]
        span_ids = candidate_ids(cards)[: params.SOURCES_MAX]  # beyond the alias cap: not sent
        retrieved.set({"reader.retrieval.hits": len(span_ids), "reader.retrieval.candidates": len(cards)})
    loaded = await store.spans_by_ids(conn, q.owner_id, span_ids) if span_ids else {}
    spans = [Span(s, *loaded[s]) for s in span_ids if s in loaded]
    if not spans:
        raise TutorError("sources_unavailable")
    return spans, bool(found["degraded"])


async def _answer(deps: Deps, conn: AsyncConnection, q: store.Question, ctx: JobContext) -> dict[str, Any]:
    """Retrieval, prompt, one call, check: the fields of the `answered` row. Raises on every other outcome."""
    engine = conn.engine
    spans, degraded = await _retrieve(deps, conn, q)
    cell = None
    if q.cell_id is not None:
        cells = await asyncio.to_thread(store.revision_cells, deps.blobs_root, q.revision_sha256)
        cell = cells.get(q.cell_id)
    prompt = build_prompt(q.question, cell, spans)
    system, prompt_sha256 = system_prompt()
    task = deps.config().tasks.get(params.TASK)
    if task is None:
        raise TutorError("tutor_failed")
    model = resolve_model(task.model)
    floor = floor_usd(model, len(system.encode("utf-8")), len(prompt.text.encode("utf-8")))
    call = StageCall(
        owner_id=q.owner_id,
        handle=q.handle,
        run_id=q.id,
        lesson_id=q.lesson_id,
        stage=params.TASK,
        task=params.TASK,
        attempt=ctx.attempts + 1,
        system=system,
        prompt=prompt.text,
        max_output_tokens=params.MAX_OUTPUT_TOKENS,
        floor_usd=floor,
        run_cap_usd=params.QUESTION_CAP_USD,
        daily_budget_usd=params.DAILY_CAP_USD,
        thinking_off=True,
        abort_on_retry=True,
        deadline_s=deps.deadline_s,
        course_id=q.course_id,
    )
    async with deps.provider(engine) as provider:
        result = await provider.run(call)
    if result.sdk_usd > floor:
        log.warning("reserve_exceeded question=%s floor=%s sdk_usd=%s", q.id, floor, result.sdk_usd)
    if result.stop_reason == "max_tokens":
        raise TutorError("output_truncated")
    with child_span("tutor.citations") as cited:  # FR-OBS-13: blocks checked, blocks supported
        checked = check(parse(result.text), prompt.sources)
        total, valid = len(checked.blocks), sum(1 for b in checked.blocks if b.supported)
        cited.set({"reader.citations.total": total, "reader.citations.valid": valid})
    # SRV-OBS FR-OBS-14: deterministic outcomes as code assessments; `record` never raises.
    counts = {"total": min(total, 1000), "valid": min(valid, 1000)}
    await feedback.record(ctx, "tutor.citations_valid", total > 0 and valid == total, counts)
    if total:
        await feedback.record(ctx, "tutor.citation_share", valid / total)
    return {
        "answer": {
            "blocks": [
                {
                    "text": b.text,
                    "supported": b.supported,
                    "quote": b.quote,
                    "citations": citations(b.kept, prompt.sources, q.course_title),
                }
                for b in checked.blocks
            ],
            "insufficient": checked.insufficient,
            "retrieval": "degraded" if degraded else "ok",
            "prompt_sha256": prompt_sha256,
            "model": result.model,
        },
        "grounded": checked.grounded,
        "model": result.model,
        "sources": len(prompt.sources),
    }


async def answer_question(deps: Deps, args: AnswerArgs, conn: AsyncConnection, ctx: JobContext) -> None:
    started = time.monotonic()
    q = await store.load(conn, args.question_id)
    if q is None or q.state != "queued":
        log.info("tutor_skip question=%s state=%s", args.question_id, q.state if q else None)
        return
    # SRV-OBS FR-OBS-5 (e), FR-OBS-12 (e), FR-OBS-18: ids on the root, the lesson as the trace's session.
    annotate_job({"reader.lesson.id": str(q.lesson_id),
                  "reader.course.id": str(q.course_id) if q.course_id else None,
                  "reader.prompt.version": await prompts.version_of("reader.tutor")})
    session_marker("reader.lesson.id", str(q.lesson_id))
    # FR-TUTOR-4: before any reservation. FR-TUTOR-21: a lesson deleted meanwhile (SRV-LESSON's `can_open`
    # hides tombstones) ends the question `expired` without a model call.
    if q.age_s > params.QUESTION_TTL_S or not await can_open(conn, q.owner_id, str(q.lesson_id)):
        written = await store.finish(conn, q.id, "expired", error_code="expired")
        annotate_job({"reader.tutor.state": "expired"})
        log.info("tutor_answer question=%s state=expired written=%s", q.id, written)
        return
    last = ctx.attempts + 1 >= params.ATTEMPTS_MAX
    code: str | None = None
    fields: dict[str, Any] = {}
    try:
        fields = await _answer(deps, conn, q, ctx)
    except TutorTransient as error:
        code = error.code
    except TutorError as error:
        code = error.code
    except AgentError as error:
        code = error.code
    except (blobs.BlobMissing, blobs.BlobCorrupt, ValueError) as error:  # the revision blob, or a bad span id
        log.warning("tutor_input_error question=%s error=%s", q.id, type(error).__name__)
        code = "tutor_failed"
    spent = await ledger.spent(conn.engine, run_id=q.id)
    seconds = time.monotonic() - started
    if code is not None and code in TRANSIENT and not last:
        stored = outcome(code)[1]
        log.info(
            "tutor_retry question=%s attempt=%s code=%s spent=%s seconds=%.1f",
            q.id, ctx.attempts + 1, stored, spent, seconds,
        )
        raise TutorTransient(stored)
    if code is not None:
        state, stored = outcome(code)
        written = await store.finish(conn, q.id, state, error_code=stored)
        annotate_job({"reader.tutor.state": state})
        log.info(
            "tutor_answer question=%s attempt=%s state=%s code=%s written=%s spent=%s seconds=%.1f",
            q.id, ctx.attempts + 1, state, stored, written, spent, seconds,
        )
        return
    # False when the row left `queued` during the call (expired or finalised by a read): not overwritten.
    written = await store.finish(
        conn, q.id, "answered", answer=fields["answer"], grounded=fields["grounded"], model=fields["model"]
    )
    annotate_job({"reader.tutor.state": "answered"})
    log.info(
        "tutor_answer question=%s attempt=%s state=answered written=%s model=%s sources=%s blocks=%s"
        " grounded=%s retrieval=%s spent=%s seconds=%.1f",
        q.id, ctx.attempts + 1, written, fields["model"], fields["sources"], len(fields["answer"]["blocks"]),
        fields["grounded"], fields["answer"]["retrieval"], spent, seconds,
    )


def make_kind(deps: Deps) -> JobKind:
    return JobKind(
        name=params.KIND,
        queue="tutor",
        args=AnswerArgs,
        key=("question_id",),
        handler=functools.partial(answer_question, deps),
        max_attempts=params.ATTEMPTS_MAX,
        transient=(TutorTransient,),
        timeout_s=params.JOB_TIMEOUT_S,
        priority=params.PRIORITY,
    )


JOB_KINDS: Final = (make_kind(default_deps()),)
