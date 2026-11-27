"""The `pipeline.stage` / `pipeline.verify` handler: one step of a compile run (SRV-PIPELINE FR-PIPELINE-5).

After `draft`, the checks run cheapest first: rules (no call), lab verification (sandbox), fact-check (model),
adversarial review (model). A check's handler decides what follows and records it on its report (`next`, with
`stop` when the run ends): the next check, `revise` (a loop, only under the loop rule, FR-PIPELINE-23), or
`findings`. The step row and that decision commit in the job's transaction; the enqueue of the next step
follows SRV-EVENTS FR-EVENTS-3 (its own commit, before the handler returns). `findings` is the last step: it
appends the open cells and computes G2. Only ids, stage names, costs and counts are logged (INV-PIPELINE-4).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

import anyio.to_thread
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from reader import blobs
from reader.agent import ledger
from reader.agent.errors import AgentError, BudgetRefused, TransientAgentError
from reader.agent.pricing import resolve_model
from reader.agent.provider import PlannedCall, StageCall
from reader.jobs import JobContext
from reader.lessons.store import write_revision
from reader.obs import feedback, prompts
from reader.obs.spans import annotate_job, child_span
from reader.pipeline import g2 as g2_mod
from reader.pipeline import notebook as nb_mod
from reader.pipeline import rules as rules_mod
from reader.pipeline import runs
from reader.pipeline import runtime as runtime_mod
from reader.pipeline.errors import InputNotReady
from reader.pipeline.findings import Problem, append_findings, problems_from, stamp_credibility, stamp_labs
from reader.pipeline.prompts import input_sha256
from reader.pipeline.reports import (
    Finding,
    Verdict,
    check_failed,
    claim_keys,
    parse_json,
    validate_fact_report,
    validate_review_report,
)
from reader.pipeline.runs import (
    CHECKS,
    Lesson,
    RunRow,
    StepRow,
    checks_on,
    enqueue_next,
    last_check,
    latest_sha,
    load_lesson,
    load_run,
    load_steps,
    model_sha,
    next_step,
    stage_model,
    step_input,
    strip_list,
)
from reader.pipeline.runtime import Runtime
from reader.pipeline.stages import decide
from reader.pipeline.verify import LabReport

log = logging.getLogger("reader.pipeline")


async def _blob(rt: Runtime, sha256: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(await anyio.to_thread.run_sync(blobs.get, rt.blobs_root, sha256))
    return data


def _verdicts(report: Mapping[str, Any] | None) -> list[Verdict]:
    return [
        Verdict(
            v["cell"],
            v["claim"],
            v["verdict"],
            tuple(v["spans"]),
            v["note"],
            v.get("forced"),
            int(v.get("credibility", 6)),
        )
        for v in (report or {}).get("verdicts", [])
    ]


def _findings(report: Mapping[str, Any] | None) -> list[Finding]:
    return [
        Finding(f["cell"], f["claim"], f["blocking"], f["text"]) for f in (report or {}).get("findings", [])
    ]


def _failing_payload(steps: Sequence[StepRow], lesson: Lesson) -> dict[str, Any]:
    """FR-PIPELINE-3 (a): the failing check's report, in the model's terms (aliases, not span ids)."""
    failing = last_check(steps)
    if failing is None:
        return {}
    report = runs.judgement(failing.report)
    back = {span.span_id: alias for alias, span in lesson.source.aliases.items()}
    if failing.stage == "fact_check":
        verdicts = [
            {k: v for k, v in item.items() if k not in ("credibility", "spans")}
            | {"spans": [back.get(s, "unknown") for s in item["spans"]]}
            for item in report.get("verdicts", [])
        ]
        return {"failed_check": "fact_check", "fact_report": verdicts}
    if failing.stage == "adversarial_review":
        return {"failed_check": "adversarial_review", "review": report.get("findings", [])}
    if failing.stage == "lab_verification":
        lab = report.get("lab_report", {})
        return {"failed_check": "lab_verification", "lab_report": {"results": lab.get("results", {})}}
    return {"failed_check": "rules", "rule_report": report.get("failures", [])}


def _user_prompt(
    stage: str, current: Mapping[str, Any] | None, lesson: Lesson, steps: Sequence[StepRow]
) -> str:
    aliases = lesson.source.aliases
    if stage == "draft":
        payload: dict[str, Any] = {"lesson_title": lesson.title, "task": "Draft this lesson from the source."}
    else:
        assert current is not None  # noqa: S101 (a notebook exists after draft)
        payload = {"notebook": nb_mod.to_compact(current, aliases)}
        if stage == "fact_check":
            payload["claims"] = [{"cell": c, "claim": k} for c, k in claim_keys(current)]
        if stage == "revise":
            payload |= _failing_payload(steps, lesson)
    return json.dumps(payload, ensure_ascii=False)


async def _model_step(
    rt: Runtime,
    run: RunRow,
    lesson: Lesson,
    steps: Sequence[StepRow],
    stage: str,
    attempt: int,
    current: dict[str, Any] | None,
    handle: str,
    step: int | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any], str, Decimal]:
    """Run a model stage: (notebook or None, the step report, model, cost)."""
    sdef = rt.pipeline.stages[stage]
    assert sdef.task is not None  # noqa: S101
    result = await rt.provider.run(
        StageCall(
            owner_id=run.owner_id,
            handle=handle,
            run_id=run.id,
            lesson_id=run.lesson_id,
            stage=stage,
            task=sdef.task,
            attempt=attempt,
            system=rt.prompts.system(stage, lesson.source),
            prompt=_user_prompt(stage, current, lesson, steps),
            max_output_tokens=sdef.max_output_tokens,
            floor_usd=sdef.floor_reserve_usd,
            run_cap_usd=run.cap_usd,
            course_id=lesson.course_id,
            step=step,
        )
    )
    aliases = lesson.source.aliases
    if stage in ("draft", "revise"):
        built = nb_mod.build(
            nb_mod.parse(result.text),
            title=lesson.title,
            aliases=aliases,
            material_title=lesson.material_title,
            stage=stage,
            prompt_version=rt.prompts.version,
            model=result.model,
            previous=current,
        )
        return (
            built.notebook,
            {"stripped": [[c, k] for c, k in sorted(built.stripped, key=lambda ck: (ck[0], ck[1] or ""))]},
            result.model,
            result.sdk_usd,
        )
    assert current is not None  # noqa: S101
    if stage == "fact_check":
        stripped = {(c, k) for c, k in strip_list(steps)}
        with child_span("verify.claims") as claims:  # SRV-OBS FR-OBS-13: counts only
            verdicts = validate_fact_report(parse_json(result.text), current, aliases, frozenset(stripped))
            claims.set({"reader.claims.total": len(verdicts),
                        "reader.claims.supported": sum(1 for v in verdicts if v.verdict == "supported")})
        report: dict[str, Any] = {
            "verdicts": [
                {
                    "cell": v.cell,
                    "claim": v.claim,
                    "verdict": v.verdict,
                    "spans": list(v.spans),
                    "note": v.note,
                    "forced": v.forced,
                    "credibility": v.credibility,
                }
                for v in verdicts
            ]
        }
    else:
        report = {
            "findings": [
                {"cell": f.cell, "claim": f.claim, "blocking": f.blocking, "text": f.text}
                for f in validate_review_report(parse_json(result.text))
            ]
        }
    return None, report, result.model, result.sdk_usd


async def _record_stage_error(
    conn: AsyncConnection, run: RunRow, step: int, input_sha: str, code: str
) -> None:
    """FR-PIPELINE-16: an allow-listed code is recorded, any other permanent code clears the record.

    Called through the `runs` module so tests can replace it; a failed write is logged (run and code only)
    and swallowed, so the caller re-raises the original exception.
    """
    value = (
        {"step": step, "input_sha256": input_sha, "code": code} if code in runs.STAGE_ERROR_CODES else None
    )
    try:
        await runs.record_stage_error(conn.engine, run.id, value)
    except Exception:  # the original failure matters, not the record's
        log.warning("compile_stage_error_not_recorded run=%s code=%s", run.id, code)


async def _cancelled(conn: AsyncConnection, run_id: uuid.UUID) -> bool:
    """FR-PIPELINE-20 (2): re-read under the run row's lock, so a cancel either committed before (seen here)
    or waits until this step has committed its revision and its enqueue."""
    row = await conn.execute(
        text("SELECT cancelled_at FROM compile_runs WHERE id = :r FOR UPDATE"), {"r": run_id}
    )
    return row.scalar() is not None


async def _handle(conn: AsyncConnection, run: RunRow) -> str:
    handle = (
        await conn.execute(text("SELECT handle FROM users WHERE id = :u"), {"u": run.owner_id})
    ).scalar()
    return str(handle)


async def _reusable(conn: AsyncConnection, run: RunRow, stage: str, input_sha: str) -> StepRow | None:
    row = (
        await conn.execute(
            text(
                "SELECT s.id, s.step, s.stage, s.input_sha256, s.output_sha256, s.report, s.reused_from,"
                " s.model FROM compile_steps s JOIN compile_runs r ON r.id = s.run_id WHERE r.lesson_id = :l"
                " AND s.owner_id = :o AND s.stage = :s AND s.input_sha256 = :h"  # this run or an earlier one
                " ORDER BY s.id LIMIT 1"
            ),
            {"l": run.lesson_id, "o": run.owner_id, "s": stage, "h": input_sha},
        )
    ).first()
    if row is None:
        return None
    return StepRow(
        row[0], row[1], row[2], row[3], row[4], row[5] or {}, row[6], row[7], Decimal(0), Decimal(0)
    )


async def _reusable_check(
    conn: AsyncConnection, rt: Runtime, run: RunRow, lesson: Lesson, stage: str, model_sha256: str
) -> bool:
    """FR-PIPELINE-23: a check of the revised notebook is unplanned only when its own input_sha256 — computed
    from the notebook an earlier check of that model-written revision judged, under the current prompts and
    model — has a completed step to reuse (the `_reusable` predicate)."""
    rows = await conn.execute(
        text(
            "SELECT DISTINCT s.report ->> 'notebook_sha256' FROM compile_steps s JOIN compile_runs r"
            " ON r.id = s.run_id WHERE r.lesson_id = :l AND s.owner_id = :o AND s.stage = :s"
            " AND s.report ->> 'model_sha256' = :m"
        ),
        {"l": run.lesson_id, "o": run.owner_id, "s": stage, "m": model_sha256},
    )
    model = stage_model(rt, stage)
    for (notebook_sha,) in rows.all():
        if notebook_sha is None:
            continue
        input_sha = input_sha256(stage, lesson.source.sha256, [str(notebook_sha)], rt.prompts, model)
        if await _reusable(conn, run, stage, input_sha) is not None:
            return True
    return False


def _planned(rt: Runtime, stage: str, prompt_chars: int) -> PlannedCall:
    sdef = rt.pipeline.stages[stage]
    assert sdef.task is not None  # noqa: S101
    # custom_system: every stage call sets its own system prompt (FR-AGENT-13), so the reserve counts the
    # CLI's overhead beside it, not its whole system prompt
    return PlannedCall(
        sdef.task, prompt_chars, sdef.max_output_tokens, sdef.floor_reserve_usd, custom_system=True
    )


def _prompt_chars(
    rt: Runtime, stage: str, current: Mapping[str, Any], lesson: Lesson, steps: Sequence[StepRow]
) -> int:
    return len(rt.prompts.system(stage, lesson.source)) + len(_user_prompt(stage, current, lesson, steps))


async def loop_plan(
    conn: AsyncConnection,
    rt: Runtime,
    run: RunRow,
    lesson: Lesson,
    steps: Sequence[StepRow],
    current: Mapping[str, Any],
) -> list[PlannedCall]:
    """FR-PIPELINE-23: the loop's calls that are not reusable, reuse checked first. `steps` includes the
    failing check's own row. Prompts of the checks are estimated from the current notebook."""
    planned: list[PlannedCall] = []
    revised: str | None = None
    reuse = await _reusable(conn, run, "revise", step_input(rt, lesson, steps, "revise"))
    if reuse is None:
        planned.append(_planned(rt, "revise", _prompt_chars(rt, "revise", current, lesson, steps)))
    else:
        revised = reuse.output_sha256
    for stage in ("fact_check", "adversarial_review"):
        # a check is droppable only while every earlier call of the loop is reused: once one runs anew, the
        # notebooks after it are new, so every later check is planned too
        if not planned and revised is not None and await _reusable_check(
            conn, rt, run, lesson, stage, revised
        ):
            continue
        planned.append(_planned(rt, stage, _prompt_chars(rt, stage, current, lesson, steps)))
    return planned


def _planned_usd(rt: Runtime, planned: Sequence[PlannedCall]) -> Decimal:
    total = Decimal(0)
    for p in planned:
        task = rt.config.tasks.get(p.task)
        if task is not None:
            total += ledger.reserve_for(
                resolve_model(task.model),
                p.prompt_chars,
                p.max_output_tokens,
                p.floor_usd,
                custom_system=p.custom_system,
                searches_max=p.searches_max,
            )
    return total


async def _decide(
    conn: AsyncConnection,
    rt: Runtime,
    run: RunRow,
    lesson: Lesson,
    steps: Sequence[StepRow],
    stage: str,
    report: dict[str, Any],
    current: Mapping[str, Any],
) -> RunRow:
    """A check's decision, recorded on its report and, for a loop or a loop_cap stop, on the run row, in the
    job's transaction (FR-PIPELINE-5, -23). Returns the run as the enqueue must see it."""
    failed = check_failed(stage, report)
    due = failed and run.loops < rt.pipeline.max_loops
    limit: str | None = None
    planned: list[PlannedCall] = []
    if due:
        row = StepRow(0, len(steps) + 1, stage, "", None, report, None, None, Decimal(0), Decimal(0))
        planned = await loop_plan(conn, rt, run, lesson, [*steps, row], current)
        if planned:  # a loop with nothing to call starts without asking
            limit = await rt.provider.loop_fits_limit(run.owner_id, run.id, run.cap_usd, planned)
    nxt, stop, refused_limit = decide(rt.pipeline, stage, failed, run.loops, lambda: limit)
    report["next"] = nxt
    if stop is not None:
        report["stop"] = stop
    if due:
        report["planned_usd"] = str(_planned_usd(rt, planned))
        report["planned_judge_usd"] = "0"  # section mode makes no judge.find call (FR-PIPELINE-26)
    if nxt == "revise":
        await conn.execute(text("UPDATE compile_runs SET loops = loops + 1 WHERE id = :r"), {"r": run.id})
        return dataclasses.replace(run, loops=run.loops + 1)
    if stop == "loop_cap":
        code = f"loop_cap_{refused_limit}"
        report["refused_limit"] = refused_limit
        await conn.execute(
            text(
                "UPDATE compile_runs SET budget_refused = coalesce(budget_refused, :c) WHERE id = :r"
                " AND cancelled_at IS NULL"
            ),
            {"c": code, "r": run.id},
        )
        log.info("compile_loop_refused run=%s limit=%s", run.id, refused_limit)
        return dataclasses.replace(run, budget_refused=run.budget_refused or code)
    return run


def _findings_problems(steps: Sequence[StepRow], notebook: Mapping[str, Any]) -> list[Problem]:
    """FR-PIPELINE-10: the problems of the stop, from the checks that ran on the final notebook."""
    ran = {s.stage: s.report for s in checks_on(steps)}
    lab = ran.get("lab_verification")
    lab_problems = LabReport.from_json(lab["lab_report"]).problems if lab else []
    return problems_from(
        _verdicts(ran.get("fact_check")),
        _findings(ran.get("adversarial_review")),
        notebook,
        rule_failures=(ran.get("rules") or {}).get("failures", []),
        lab_problems=lab_problems,
        not_run=[c for c in CHECKS if c not in ran],
    )


async def handle(args: Any, conn: AsyncConnection, ctx: JobContext) -> None:
    started = time.monotonic()
    rt = runtime_mod.get(conn.engine)
    run_id = uuid.UUID(args.run_id)
    run = await load_run(conn, run_id)
    if run is None:
        raise InputNotReady()
    # FR-PIPELINE-20 (1), revised: before any reservation, model call or lab run; v1 runs never run a step.
    if run.cancelled_at is not None or run.order_version < runs.ORDER_VERSION:
        log.info("compile_step_skipped run=%s step=%s", run.id, args.step)
        return
    steps = await load_steps(conn, run_id)
    lesson = await load_lesson(conn, rt, run.owner_id, run.lesson_id)
    # SRV-OBS FR-OBS-5 (e), FR-OBS-18: ids on the job's root; the session marker came from the wrapper.
    version = await prompts.version_of(f"reader.{args.stage}")
    annotate_job({"reader.lesson.id": str(run.lesson_id),
                  "reader.course.id": str(lesson.course_id) if lesson.course_id else None,
                  "reader.prompt.version": version})
    jobs = rt.jobs()
    if any(s.step == args.step for s in steps):  # already recorded: only make sure the run moves on
        await enqueue_next(jobs, rt, run, lesson, steps)
        return
    stage = args.stage
    model = stage_model(rt, stage)
    if run.budget_refused and model is not None:  # a refusal wins over `next`: a stale model step
        log.info("compile_step_stale run=%s step=%s", run.id, args.step)
        await enqueue_next(jobs, rt, run, lesson, steps)
        return
    if stage == "findings" and len(steps) == args.step - 1 and next_step(rt, run, steps) != "findings":
        # the decision admitted a loop: no effect, and the key must not complete (FR-PIPELINE-5)
        log.info("compile_step_stale run=%s step=%s", run.id, args.step)
        raise InputNotReady()
    expected = step_input(rt, lesson, steps, stage, run.budget_refused)
    if len(steps) != args.step - 1 or expected != args.input_sha256:
        raise InputNotReady()
    m = model_sha(steps)
    latest = latest_sha(steps)
    current = await _blob(rt, latest) if latest else None
    notebook: dict[str, Any] | None
    reused_from: int | None = None
    cost = Decimal(0)
    if model is not None:
        reuse = await _reusable(conn, run, stage, args.input_sha256)
        if reuse is not None:
            reused_from, report = reuse.id, runs.judgement(reuse.report)
            notebook = (
                await _blob(rt, reuse.output_sha256)
                if stage in ("draft", "revise") and reuse.output_sha256
                else None
            )
        else:
            try:
                notebook, report, model, cost = await _model_step(
                    rt, run, lesson, steps, stage, ctx.attempts, current, await _handle(conn, run), args.step
                )
                if stage == "fact_check":  # FR-OBS-14; `record` never raises
                    claims = len(report.get("verdicts", []))
                    supported = sum(1 for v in report.get("verdicts", []) if v["verdict"] == "supported")
                    counts = {"claims": min(claims, 10000), "supported": min(supported, 10000)}
                    await feedback.record(ctx, "fact_check.passed", supported == claims, counts)
            except BudgetRefused as refused:
                await _refused(conn, jobs, rt, run, lesson, steps, refused.code)
                log.info("compile_budget_refused run=%s stage=%s code=%s", run.id, stage, refused.code)
                return
            except TransientAgentError:
                raise
            except AgentError as failure:  # FR-PIPELINE-16: a permanent code, recorded for the status routes
                await _record_stage_error(conn, run, args.step, args.input_sha256, failure.code)
                raise
        if stage == "fact_check":
            assert current is not None  # noqa: S101
            notebook = stamp_credibility(current, _verdicts(report))  # FR-PIPELINE-27
        elif notebook is None:
            notebook = current
    elif stage == "rules":
        assert current is not None  # noqa: S101
        notebook = current
        reuse = await _reusable(conn, run, stage, args.input_sha256)  # FR-PIPELINE-5: rules reuse too
        if reuse is not None:
            reused_from, report = reuse.id, runs.judgement(reuse.report)
        else:
            data = rules_mod.RuleInput(
                current,
                {span.span_id: span for span in lesson.source.aliases.values()},
                {(c, k) for c, k in strip_list(steps)},
                rt.pipeline.reference_libraries,
            )
            report = rules_mod.report(rt.pipeline.rules, data)
    elif stage == "findings":
        assert current is not None  # noqa: S101
        problems = _findings_problems(steps, current)
        notebook = append_findings(current, problems)
        report = {"problems": [p.as_json() for p in problems], "model_sha256": m}
    else:  # lab_verification
        assert current is not None  # noqa: S101
        lab_started = time.monotonic()
        with child_span("verify.lab") as lab:  # FR-OBS-13: cells, cells passed, milliseconds
            lab_report = await rt.verify.run(current)
            cells = len(lab_report.results)
            cells_ok = sum(1 for r in lab_report.results.values() if r.get("passed"))
            lab.set({"reader.kernel.cells": cells, "reader.kernel.cells_ok": cells_ok,
                     "reader.kernel.ms": int((time.monotonic() - lab_started) * 1000)})
        await feedback.record(ctx, "verify.lab_passed", cells_ok == cells,
                              {"cells": min(cells, 10000), "cells_ok": min(cells_ok, 10000)})
        await feedback.record(ctx, "verify.passed", not lab_report.problems)
        notebook = stamp_labs(current, lab_report)
        report = {"lab_report": lab_report.as_json()}
    assert notebook is not None  # noqa: S101
    if stage in CHECKS:
        report["notebook_sha256"] = latest
        report["model_sha256"] = m
    if await _cancelled(conn, run.id):  # FR-PIPELINE-20 (2): holds the run row until this step commits
        log.info("compile_step_cancelled run=%s step=%s", run.id, args.step)
        return
    if stage in CHECKS:
        assert current is not None  # noqa: S101
        run = await _decide(conn, rt, run, lesson, steps, stage, report, current)
    duration = round(time.monotonic() - started, 3)
    provenance: dict[str, Any] = {
        "input_sha256": args.input_sha256,
        "cost_usd": float(cost),
        "duration_s": duration,
    }
    if model is not None:
        provenance |= {"model": model, "prompt_version": rt.prompts.version}
    sha, _ = await write_revision(
        conn, rt.blobs_root, run.owner_id, run.lesson_id, nb_mod.to_bytes(notebook), stage, provenance
    )
    step_id = (
        await conn.execute(
            text(
                "INSERT INTO compile_steps (owner_id, run_id, step, stage, input_sha256, output_sha256,"
                " report, reused_from, model, cost_usd, duration_s) VALUES (:o, :r, :n, :s, :h, :out,"
                " CAST(:rep AS jsonb), :ru, :m, :c, :d) RETURNING id"
            ),
            {
                "o": run.owner_id,
                "r": run.id,
                "n": args.step,
                "s": stage,
                "h": args.input_sha256,
                "out": sha,
                "rep": json.dumps(report, ensure_ascii=False),
                "ru": reused_from,
                "m": model,
                "c": cost,
                "d": Decimal(str(duration)),
            },
        )
    ).scalar_one()
    steps = [
        *steps,
        StepRow(
            step_id,
            args.step,
            stage,
            args.input_sha256,
            sha,
            report,
            reused_from,
            model,
            cost,
            Decimal(str(duration)),
        ),
    ]
    if stage == "findings":
        await _finish(conn, rt, run, steps, notebook, sha)
    log.info(
        "compile_step run=%s step=%s stage=%s reused=%s cost=%s",
        run.id,
        args.step,
        stage,
        reused_from is not None,
        cost,
    )
    await enqueue_next(jobs, rt, run, lesson, steps)


async def _refused(
    conn: AsyncConnection,
    jobs: Any,
    rt: Runtime,
    run: RunRow,
    lesson: Lesson,
    steps: Sequence[StepRow],
    code: str,
) -> None:
    """A model step refused for budget writes no compile_steps row: it records `budget_refused` and enqueues
    `findings`, or finishes the run when no notebook exists yet (FR-PIPELINE-5). The refusal (and the finish)
    commit on their own connection, before the enqueue, so a rollback of the job's transaction cannot leave a
    findings job without its refusal. `conn` is the job's connection; nothing is written on it."""
    async with conn.engine.begin() as own:
        updated = await own.execute(
            text(
                "UPDATE compile_runs SET budget_refused = coalesce(budget_refused, :c) WHERE id = :r"
                " AND cancelled_at IS NULL RETURNING budget_refused"  # FR-PIPELINE-20: a cancelled run stays
            ),
            {"c": code, "r": run.id},
        )
        recorded = updated.scalar()
        if recorded is None:
            return
        if not any(s.stage in ("draft", "revise") for s in steps):  # no notebook: nothing more to do
            await own.execute(
                text(
                    "UPDATE compile_runs SET finished_at = now(), g2 = CAST(:g AS jsonb) WHERE id = :r"
                    " AND cancelled_at IS NULL"
                ),
                {"r": run.id, "g": json.dumps({"publishable": False, "failing": [], "blocking_open": 0})},
            )
            return
    await enqueue_next(jobs, rt, dataclasses.replace(run, budget_refused=str(recorded)), lesson, steps)


def _blocking_open(notebook: Mapping[str, Any]) -> int:
    count = 0
    for cell in notebook.get("cells", []):
        reader = cell.get("metadata", {}).get("reader", {})
        finding = reader.get("finding")
        if (
            reader.get("kind") == "open"
            and isinstance(finding, dict)
            and finding.get("blocking")
            and reader.get("status") != "answered"
        ):
            count += 1
    return count


async def _finish(
    conn: AsyncConnection,
    rt: Runtime,
    run: RunRow,
    steps: Sequence[StepRow],
    final: dict[str, Any],
    final_sha: str,
) -> None:
    """G2, computed by the findings step (FR-PIPELINE-8); the run's terminal state follows (FR-PIPELINE-6)."""
    m = model_sha(steps)
    assert m is not None  # noqa: S101
    model_written = await _blob(rt, m)
    problems = [Problem.from_json(p) for p in steps[-1].report.get("problems", [])]
    ran = {s.stage: s.report for s in checks_on(steps)}
    lab = ran.get("lab_verification")
    lab_report = LabReport.from_json(lab["lab_report"]) if lab else None
    verdicts = _verdicts(ran["fact_check"]) if "fact_check" in ran else None
    labbed, credited = g2_mod.derive(model_written, lab_report, verdicts)
    expected = {  # (c): each check's input notebook is M's deterministic derivation
        "rules": m,
        "lab_verification": m,
        "fact_check": g2_mod.canonical_sha256(labbed),
        "adversarial_review": g2_mod.canonical_sha256(credited),
    }
    missing = [c for c in CHECKS if c not in ran or ran[c].get("notebook_sha256") != expected[c]]
    by_key = {(v.cell, v.claim): v.verdict for v in verdicts or []}
    deltas = g2_mod.deltas_ok(final, model_written, problems, lab_report, verdicts)
    report = g2_mod.report(final, missing, lab_report, by_key, deltas)
    report["blocking_open"] = _blocking_open(final)
    await conn.execute(
        text(
            "UPDATE compile_runs SET finished_at = now(), g2 = CAST(:g AS jsonb), final_sha256 = :f,"
            " last_model_sha256 = :m WHERE id = :r AND cancelled_at IS NULL"  # FR-PIPELINE-20
        ),
        {"g": json.dumps(report), "f": final_sha, "m": m, "r": run.id},
    )
    log.info(
        "compile_finished run=%s publishable=%s blocking_open=%s",
        run.id,
        report["publishable"],
        report["blocking_open"],
    )
