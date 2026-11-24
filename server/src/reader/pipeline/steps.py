"""The `pipeline.stage` / `pipeline.verify` handler: one step of a compile run (SRV-PIPELINE FR-PIPELINE-5).

A handler reads its input (absent → `input_not_ready`, transient), reuses an earlier step of the lesson
with the same `input_sha256` (no provider call) or runs the stage, writes a revision through SRV-LESSON,
records the step, finishes the run after lab verification, and enqueues the next step before it returns.
Only ids, stage names, costs and counts are logged (INV-PIPELINE-4).
"""

from __future__ import annotations

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
from reader.agent.errors import AgentError, BudgetRefused, TransientAgentError
from reader.agent.provider import StageCall
from reader.jobs import JobContext
from reader.lessons.store import write_revision
from reader.obs import feedback, prompts
from reader.obs.spans import annotate_job, child_span
from reader.pipeline import g2 as g2_mod
from reader.pipeline import notebook as nb_mod
from reader.pipeline import runs
from reader.pipeline import runtime as runtime_mod
from reader.pipeline.errors import InputNotReady
from reader.pipeline.findings import Problem, append_findings, problems_from, stamp_labs
from reader.pipeline.reports import Finding, Verdict, parse_json, validate_fact_report, validate_review_report
from reader.pipeline.runs import (
    Lesson,
    RunRow,
    StepRow,
    enqueue_next,
    last_report,
    latest_sha,
    load_lesson,
    load_run,
    load_steps,
    model_sha,
    stage_model,
    step_input,
)
from reader.pipeline.runtime import Runtime
from reader.pipeline.verify import LabReport

log = logging.getLogger("reader.pipeline")


async def _blob(rt: Runtime, sha256: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(await anyio.to_thread.run_sync(blobs.get, rt.blobs_root, sha256))
    return data


def _verdicts(report: Mapping[str, Any] | None) -> list[Verdict]:
    return [
        Verdict(v["cell"], v["claim"], v["verdict"], tuple(v["spans"]), v["note"], v.get("forced"))
        for v in (report or {}).get("verdicts", [])
    ]


def _findings(report: Mapping[str, Any] | None) -> list[Finding]:
    return [
        Finding(f["cell"], f["claim"], f["blocking"], f["text"]) for f in (report or {}).get("findings", [])
    ]


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
            from reader.pipeline.reports import claim_keys

            payload["claims"] = [{"cell": c, "claim": k} for c, k in claim_keys(current)]
        if stage == "revise":
            back = {span.span_id: alias for alias, span in aliases.items()}
            payload["fact_report"] = [
                {**v, "spans": [back.get(s, "unknown") for s in v["spans"]]}
                for v in (last_report(steps, "fact_check") or {}).get("verdicts", [])
            ]
            payload["review"] = (last_report(steps, "adversarial_review") or {}).get("findings", [])
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
        producer = next(s for s in reversed(steps) if s.stage in ("draft", "revise"))
        stripped = {(c, k) for c, k in producer.report.get("stripped", [])}
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


async def handle(args: Any, conn: AsyncConnection, ctx: JobContext) -> None:
    started = time.monotonic()
    rt = runtime_mod.get(conn.engine)
    run_id = uuid.UUID(args.run_id)
    run = await load_run(conn, run_id)
    if run is None:
        raise InputNotReady()
    if run.cancelled_at is not None:  # FR-PIPELINE-20 (1): before any reservation, model call or lab run
        log.info("compile_step_cancelled run=%s step=%s", run.id, args.step)
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
    if len(steps) != args.step - 1 or step_input(rt, lesson, steps, args.stage) != args.input_sha256:
        raise InputNotReady()
    stage = args.stage
    model = stage_model(rt, stage)
    m = model_sha(steps)
    current = await _blob(rt, m) if m else None
    notebook: dict[str, Any] | None
    reused_from: int | None = None
    cost = Decimal(0)
    if model is not None:
        reuse = await _reusable(conn, run, stage, args.input_sha256)
        if reuse is not None:
            reused_from, report = reuse.id, dict(reuse.report)
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
        if notebook is None:
            notebook = current
        if stage in ("fact_check", "adversarial_review"):
            report["notebook_sha256"] = m
    elif stage == "findings":
        assert current is not None  # noqa: S101
        problems = problems_from(
            _verdicts(last_report(steps, "fact_check")),
            _findings(last_report(steps, "adversarial_review")),
            current,
        )
        notebook = append_findings(current, problems)
        report = {"problems": [p.as_json() for p in problems], "model_sha256": m}
    else:  # lab_verification
        latest = latest_sha(steps)
        assert latest is not None  # noqa: S101
        before = await _blob(rt, latest)
        lab_started = time.monotonic()
        with child_span("verify.lab") as lab:  # FR-OBS-13: cells, cells passed, milliseconds
            lab_report = await rt.verify.run(before)
            cells = len(lab_report.results)
            cells_ok = sum(1 for r in lab_report.results.values() if r.get("passed"))
            lab.set({"reader.kernel.cells": cells, "reader.kernel.cells_ok": cells_ok,
                     "reader.kernel.ms": int((time.monotonic() - lab_started) * 1000)})
        await feedback.record(ctx, "verify.lab_passed", cells_ok == cells,
                              {"cells": min(cells, 10000), "cells_ok": min(cells_ok, 10000)})
        await feedback.record(ctx, "verify.passed", not lab_report.problems)
        notebook = stamp_labs(before, lab_report)
        report = {"lab_report": lab_report.as_json()}
    assert notebook is not None  # noqa: S101
    if await _cancelled(conn, run.id):  # FR-PIPELINE-20 (2): holds the run row until this step commits
        log.info("compile_step_cancelled run=%s step=%s", run.id, args.step)
        return
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
    if stage == "revise":
        await conn.execute(text("UPDATE compile_runs SET loops = loops + 1 WHERE id = :r"), {"r": run.id})
    if stage == "lab_verification":
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
    updated = await conn.execute(
        text(
            "UPDATE compile_runs SET budget_refused = coalesce(budget_refused, :c) WHERE id = :r"
            " AND cancelled_at IS NULL RETURNING id"  # FR-PIPELINE-20: a cancelled run is left as it is
        ),
        {"c": code, "r": run.id},
    )
    if updated.first() is None:
        return
    refused = RunRow(
        run.id, run.owner_id, run.lesson_id, run.cap_usd, run.task_budgets, code, None, None, None
    )
    if not any(s.stage in ("draft", "revise") for s in steps):  # no notebook: nothing more to do
        await conn.execute(
            text(
                "UPDATE compile_runs SET finished_at = now(), g2 = CAST(:g AS jsonb) WHERE id = :r"
                " AND cancelled_at IS NULL"
            ),
            {"r": run.id, "g": json.dumps({"publishable": False, "failing": [], "blocking_open": 0})},
        )
        return
    await enqueue_next(jobs, rt, refused, lesson, steps)


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
    """G2 after lab verification (FR-PIPELINE-8); the run's terminal state follows from it (FR-PIPELINE-6)."""
    m = model_sha(steps)
    assert m is not None  # noqa: S101
    model_written = await _blob(rt, m)
    findings_report = last_report(steps, "findings") or {}
    problems = [Problem.from_json(p) for p in findings_report.get("problems", [])]
    lab_report = LabReport.from_json((last_report(steps, "lab_verification") or {})["lab_report"])
    records = {
        r[0]
        for r in await conn.execute(
            text("SELECT stage FROM lesson_revision_stages WHERE lesson_id = :l AND sha256 = :s"),
            {"l": run.lesson_id, "s": m},
        )
    }
    verdicts = {
        (v["cell"], v["claim"]): v["verdict"]
        for v in (last_report(steps, "fact_check") or {}).get("verdicts", [])
    }
    deltas = g2_mod.deltas_ok(final, model_written, problems, lab_report)
    report = g2_mod.report(final, records, lab_report, verdicts, deltas)
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
