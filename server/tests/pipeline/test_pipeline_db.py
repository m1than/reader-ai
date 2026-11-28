# @trace VEC-PIPELINE-2, FR-PIPELINE-5, FR-PIPELINE-6, FR-PIPELINE-8, FR-PIPELINE-12, FR-PIPELINE-23, FR-PIPELINE-24, FR-PIPELINE-27, INV-PIPELINE-1, INV-PIPELINE-2, INV-PIPELINE-3, INV-PIPELINE-4
"""The compile loop end to end on the real job machinery, in the four-check order: each VEC-PIPELINE-2 case and
recompile, run state, stop and loops, provenance, citations, credibility and logs. [db]"""

from __future__ import annotations

import copy
import json
import logging
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from .conftest import (
    CANARY,
    FIXTURES,
    MATERIAL,
    Pg,
    fresh_pg,
    lab_modes,
    load_yaml,
    need,
    pipeline_env,
    scripts_for,
    synthetic_spans,
)

pytestmark = pytest.mark.anyio
CASES = load_yaml(FIXTURES / "loop-cases.yaml")
DEFAULTS = CASES["defaults"]
BY_NAME = {c["name"]: c for c in CASES["cases"]}


def _env_kwargs(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_budgets": case.get("task_budgets", DEFAULTS["task_budgets"]),
        "labs": case.get("labs", "pass"),
        "floors": DEFAULTS["reserves"],
    }


async def _open_cells(env: Any, sha256: str) -> dict[str, int]:
    nb = await env.notebook(sha256)
    counts = {"blocking": 0, "nonblocking": 0}
    for cell in nb["cells"]:
        reader = cell["metadata"]["reader"]
        if reader["kind"] == "open" and reader.get("provenance", {}).get("stage") in (
            "findings",
            "lab_verification",
        ):
            counts["blocking" if reader["finding"]["blocking"] else "nonblocking"] += 1
    return counts


async def _spent(env: Any, run: uuid.UUID) -> Decimal:
    from reader.agent import ledger

    return await ledger.spent(env.engine, run_id=run)  # type: ignore[no-any-return]


class _Counting:
    """Wraps the provider's loop_fits_limit and counts the asks (FR-PIPELINE-23)."""

    def __init__(self, real: Any) -> None:
        self.real, self.calls = real, 0
        self.planned: list[list[str]] = []

    async def __call__(self, *args: Any) -> Any:
        self.calls += 1
        self.planned.append([p.task for p in args[3]])
        return await self.real(*args)


async def _prior_spend(env: Any, task: str, usd: float, cap: Decimal = Decimal(100)) -> None:
    from reader.agent import ledger

    caps = ledger.Caps(cap, None, None, None)
    rid = await ledger.reserve(
        env.engine,
        ledger.Scope(env.users["ivan"], uuid.uuid4(), env.lesson, "x", task, 0),
        "claude-opus-5-5",
        Decimal(str(usd)),
        caps,
    )
    await ledger.settle(
        env.engine,
        rid,
        Decimal(str(usd)),
        Decimal(str(usd)),
        Decimal(str(usd)),
        None,
        ledger.UpstreamRecord((200,), ()),
    )


async def _run_case(env: Any, case: dict[str, Any]) -> tuple[uuid.UUID, dict[str, Any]]:
    env.cli.scripts[:] = scripts_for(case["results"], case.get("labs", "pass"))
    env.verify.modes[:] = lab_modes(case["results"])
    if not env.lesson:
        await env.import_plan()
    for task, usd in (case.get("prior_runs_spent") or {}).items():
        await _prior_spend(env, task, usd)
    run = await env.compile(str(case.get("cap", DEFAULTS["cap"])))
    for _ in range(6):  # the worker drains; transient retries are re-enqueued with no delay
        await env.work()
    return run, await env.state(run)


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda c: c["name"])
async def test_loop_case(
    pg: Pg, tmp_path: Path, case: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    async with pipeline_env(pg, tmp_path, **_env_kwargs(case)) as env:
        run, status = await _run_case(env, case)
        assert status["state"] == case["expect_state"], status
        assert [s["stage"] for s in status["steps"]] == case["expect_stages"]
        assert env.cli.scripts == []  # every scripted model call happened, no more
        assert env.verify.modes == []  # every lab outcome was used
        if "expect_stop" in case:
            assert status["stop"] == case["expect_stop"]
            assert status["loops"] == case["expect_loops"]
            assert status["target_usd"] == "4.35"
        limits = {"loop_cap_run": "run", "loop_cap_task": "task", "budget_exhausted": "run"}
        if case.get("expect_refusals"):
            assert status["refused_limit"] == limits[case["expect_refusals"][0]]
        # FR-PIPELINE-5: a refused model step writes no compile_steps row (a dead step is listed as pending)
        recorded = len(case["expect_stages"]) - (case["expect_state"] == "stage_failed")
        assert await env.scalar("SELECT count(*) FROM compile_steps WHERE run_id = :r", r=run) == recorded
        if "expect_refusals" in case:
            assert status["refusals"] == case["expect_refusals"]
        if "expect_spent" in case:
            assert await _spent(env, run) == Decimal(str(case["expect_spent"]))
        if status["latest_sha256"] and case["expect_stages"]:
            assert await _open_cells(env, status["latest_sha256"]) == case["expect_open_cells"]
        else:
            assert case["expect_open_cells"] == {"blocking": 0, "nonblocking": 0}
        settled = sum((Decimal(str(r["cost"])) for r in case["results"] if "cost" in r), Decimal(0))
        if case["expect_state"] != "stage_failed" and not any("error" in r for r in case["results"]):
            assert await _spent(env, run) == settled  # INV-PIPELINE-1: spend = the reported costs, within cap
        assert await _spent(env, run) <= Decimal(str(case.get("cap", DEFAULTS["cap"])))
        # FR-PIPELINE-21 rows carry FR-PIPELINE-6's loops, stop and refused_limit
        runs = need("reader.pipeline.runs")
        (row,) = [r for r in await runs.history(env.engine, env.jobs, env.users["ivan"], env.lesson) if r["run_id"] == str(run)]
        assert (row["loops"], row["stop"], row["refused_limit"]) == (
            status["loops"], status["stop"], status["refused_limit"]
        )
        if case["name"] == "clean-first-pass":  # FR-PIPELINE-8: every check ran on M, nothing open
            assert status["g2"]["publishable"] is True, status["g2"]
        # INV-PIPELINE-4: no source text, prompt or output in any log line
        assert CANARY not in "\n".join(r.getMessage() for r in caplog.records)
        assert "Claim 1." not in "\n".join(r.getMessage() for r in caplog.records)


def _r3_form(spans: list[Any]) -> list[Any]:
    """The same synthetic spans as an r3 pack gives them: locators with their quote hashes (r3_gates_revision)."""
    source, locator = need("reader.pipeline.source"), need("reader.ingest.locator")
    out: list[Any] = []
    for page in sorted({s.page for s in spans}):
        on_page = [s for s in spans if s.page == page]
        ids = locator.page_locators(MATERIAL, page, [s.text for s in on_page])
        out += [
            source.Span(i, s.page, s.kind, s.text, locator.quote_sha256(s.text))
            for i, s in zip(ids, on_page, strict=True)
        ]
    return out


@pytest.mark.parametrize("form", ["legacy", "r3"])
async def test_provenance_and_citations_of_every_revision(pg: Pg, tmp_path: Path, form: str) -> None:
    """FR-PIPELINE-12 and INV-PIPELINE-3 on a run that fails each check once (stop loop_limit), over a legacy-id
    source and over a locator source (r3_gates_revision: a locator citation carries its span's quote hash)."""
    case = BY_NAME["four-checks-fail-once"]
    async with pipeline_env(pg, tmp_path, **_env_kwargs(case)) as env:
        if form == "r3":
            env.spans.spans = _r3_form(env.spans.spans)
        run, status = await _run_case(env, case)
        records = await env.rows(
            "SELECT stage, provenance, sha256 FROM lesson_revision_stages WHERE lesson_id = :l ORDER BY id",
            l=env.lesson,
        )
        assert [r[0] for r in records] == case["expect_stages"]
        for _, provenance, _sha in records:
            assert set(provenance) <= {"input_sha256", "prompt_version", "model", "cost_usd", "duration_s"}
            assert {"input_sha256", "cost_usd", "duration_s"} <= set(provenance)
        models = {stage: provenance.get("model") for stage, provenance, _ in records}
        assert models["draft"] == "claude-opus-5-5" and models["fact_check"] == "claude-sonnet-5-5"
        assert models["rules"] is None and models["lab_verification"] is None  # no model call
        # FR-PIPELINE-24: the rule check spends nothing
        assert await env.scalar(
            "SELECT count(*) FROM agent_spend WHERE run_id = :r AND stage = 'rules'", r=run
        ) == 0
        # FR-PIPELINE-27: each fact_check revision differs from its input only in credibility, on every item
        for i, (stage, _, sha) in enumerate(records):
            if stage != "fact_check":
                continue
            before, after = await env.notebook(records[i - 1][2]), await env.notebook(sha)
            for cell in after["cells"]:
                for claim in cell["metadata"]["reader"].get("claims", []):
                    assert claim["credibility"] in (2, 6, 5)
            stripped = json.loads(json.dumps(after))
            for cell in stripped["cells"]:
                for claim in cell["metadata"]["reader"].get("claims", []):
                    claim.pop("credibility")
            assert stripped == before
        spans = {s.span_id: s for s in env.spans.spans}
        cited = 0
        for _, _, sha in records:
            nb = await env.notebook(sha)
            for cell in nb["cells"]:
                for claim in cell["metadata"]["reader"].get("claims", []):
                    for citation in claim["citations"]:
                        if "span" in citation:
                            span = spans[citation["span"]]
                            assert citation["page"] == span.page
                            assert citation["material_title"] == "Synthetic course"  # the course title
                            if form == "r3":
                                assert citation["quote_sha256"] == span.quote_sha256
                            else:
                                assert "quote_sha256" not in citation
                            cited += 1
        assert cited > 0
        assert status["g2"]["publishable"] is False
        assert status["spent_usd"] == str(await _spent(env, run))
        assert status["task_budgets"] == {"draft": "4.0", "fact_check": "2.0", "review": "4.0"}


async def test_recompiles_reuse_unchanged_steps(pg_container: tuple[Pg, Any], tmp_path: Path) -> None:
    """INV-PIPELINE-2: a model step (or a rule check) never runs again when an earlier step of the lesson had its
    input; a loop whose calls are all reusable starts without loop_fits_limit (FR-PIPELINE-23)."""
    prior = BY_NAME["one-loop-converges"]
    for recompile in CASES["recompiles"]:
        sub = tmp_path / recompile["name"]
        sub.mkdir()
        async with pipeline_env(fresh_pg(pg_container), sub, **_env_kwargs(prior)) as env:
            await _run_case(env, prior)
            changed = recompile["changed"]
            for stage in ("adversarial_review", "fact_check"):
                if f"{stage}.prompt_version" in changed:
                    p = env.runtime.prompts
                    env.runtime.prompts = p.with_text(stage, p.text(stage) + "\nNew rule.")
            if "source" in changed:
                env.spans.spans[:] = synthetic_spans(extra=" (edited)")
            results = recompile.get("results", prior["results"] if "source" in changed else [])
            env.cli.scripts[:] = scripts_for(results, "pass")
            env.verify.modes[:] = lab_modes(results) if "source" in changed else []
            calls_before = len(env.cli.requests)
            counting = _Counting(env.runtime.provider.loop_fits_limit)
            env.runtime.provider.loop_fits_limit = counting
            if recompile["name"] == "recompile-unchanged":  # the month is spent to the cap: asking would refuse
                await _prior_spend(env, "draft", 1000.0, cap=Decimal(2000))
            run = await env.compile()
            for _ in range(6):
                await env.work()
            status = await env.state(run)
            reused = [s["stage"] for s in status["steps"] if s["reused"]]
            assert reused == recompile["expect_reused"], recompile["name"]
            if "expect_provider_calls" in recompile:
                assert len(env.cli.requests) - calls_before == recompile["expect_provider_calls"]
            if "expect_spent" in recompile:
                assert await _spent(env, run) == Decimal(str(recompile["expect_spent"]))
            if "expect_state" in recompile:
                assert status["state"] == recompile["expect_state"]
            if recompile["name"] == "recompile-unchanged":
                assert counting.calls == 0  # every call of the loop was reusable: no ask, even at the month cap
            if "expect_planned" in recompile:  # FR-PIPELINE-23: the loop plan, reuse checked first
                assert counting.planned == recompile["expect_planned"], recompile["name"]


async def test_converged_run_with_a_finding_has_no_deltas_mismatch(pg: Pg, tmp_path: Path) -> None:
    """FR-PIPELINE-8 (d) on the stored revision: a converged run that appends one non-blocking open cell keeps
    deltas clean (V.live 2026-11-23 reported deltas.mismatch on every run that appended a findings cell)."""
    case = copy.deepcopy(BY_NAME["clean-first-pass"])
    case["results"][2]["nonblocking"] = 1
    async with pipeline_env(pg, tmp_path, **_env_kwargs(case)) as env:
        _, status = await _run_case(env, case)
        assert status["state"] == "converged", status
        assert await _open_cells(env, status["latest_sha256"]) == {"blocking": 0, "nonblocking": 1}
        assert status["g2"] is not None
        assert "deltas.mismatch" not in status["g2"]["failing"], status["g2"]


async def test_stalled_run_is_resumed_by_ensure(pg: Pg, tmp_path: Path) -> None:
    """FR-PIPELINE-6: a lost enqueue leaves the run `stalled`; ensure re-enqueues its next step."""
    case = BY_NAME["clean-first-pass"]
    async with pipeline_env(pg, tmp_path, **_env_kwargs(case)) as env:
        env.cli.scripts[:] = scripts_for(case["results"], "pass")
        await env.import_plan()
        run = await env.compile()
        await env.app.job_manager.connector.execute_query_async(
            "DELETE FROM procrastinate_jobs WHERE status = 'todo'"
        )
        from sqlalchemy import text

        async with env.engine.begin() as conn:  # the lost enqueue: no key row, no job
            await conn.execute(text("DELETE FROM job_keys"))
        runs = need("reader.pipeline.runs")
        assert (await runs.derive(env.engine, env.jobs, env.users["ivan"], run))["state"] == "stalled"
        status = await env.state(run)  # the status read calls ensure
        assert status["state"] == "running"
        for _ in range(3):
            await env.work()
        assert (await env.state(run))["state"] == "converged"


async def test_one_run_per_lesson(pg: Pg, tmp_path: Path) -> None:
    errors = need("reader.pipeline.errors")
    async with pipeline_env(pg, tmp_path) as env:
        await env.import_plan()
        await env.compile()
        with pytest.raises(errors.PipelineError) as refused:
            await env.compile()
        assert refused.value.code == "compile_running"


async def test_status_is_filtered_by_owner(pg: Pg, tmp_path: Path) -> None:
    """The owner filter on its own (the routes' role check is tested in test_routes_db)."""
    errors = need("reader.pipeline.errors")
    runs = need("reader.pipeline.runs")
    async with pipeline_env(pg, tmp_path) as env:
        await env.import_plan()
        run = await env.compile()
        with pytest.raises(errors.PipelineError) as refused:
            await runs.status(env.engine, env.jobs, env.users["anna"], run)
        assert refused.value.code == "not_found"


async def test_reuse_lookup_includes_the_same_run(pg: Pg, tmp_path: Path) -> None:
    """A step of this run with the same input_sha256 is reusable too, not only earlier runs' steps."""
    steps = need("reader.pipeline.steps")
    runs = need("reader.pipeline.runs")
    case = BY_NAME["clean-first-pass"]
    async with pipeline_env(pg, tmp_path, **_env_kwargs(case)) as env:
        run, _ = await _run_case(env, case)
        async with env.engine.connect() as conn:
            row = await runs.load_run(conn, run)
            done = await runs.load_steps(conn, run)
            fact = next(s for s in done if s.stage == "fact_check")
            found = await steps._reusable(conn, row, "fact_check", fact.input_sha256)
        assert found is not None
        assert found.id == fact.id


def test_wontfix_still_blocks_in_the_derived_state() -> None:
    steps = need("reader.pipeline.steps")
    nb = json.loads((FIXTURES.parent / "SRV-LESSON" / "F01.ipynb").read_text())
    opened = next(c for c in nb["cells"] if c["metadata"]["reader"]["kind"] == "open")
    assert steps._blocking_open(nb) == 0
    opened["metadata"]["reader"]["finding"]["blocking"] = True
    for status, blocking in (("open", 1), ("wontfix", 1), ("answered", 0)):
        opened["metadata"]["reader"]["status"] = status
        assert steps._blocking_open(nb) == blocking, status


async def test_source_comes_from_the_lessons_page_range(pg: Pg, tmp_path: Path) -> None:
    source = need("reader.pipeline.source")
    async with pipeline_env(pg, tmp_path) as env:
        await env.import_plan()
        async with env.engine.connect() as conn:
            spans, title = await source.DbSpanReader().load(conn, env.users["ivan"], MATERIAL, 61, 61)
        assert title == "synthetic-1"
        assert [s.span_id for s in spans] == [f"{MATERIAL}:{i}" for i in range(3)]
        assert all(s.page == 61 for s in spans)
        payload = json.dumps([s.text for s in spans])
        assert CANARY in payload


async def test_g2_stages_need_the_derivation_of_m(pg: Pg, tmp_path: Path) -> None:
    """FR-PIPELINE-8 (c) revised: a check counts on M only when the notebook it judged is M's deterministic
    derivation; the same run's report naming another notebook makes that check missing."""
    steps_mod = need("reader.pipeline.steps")
    runs = need("reader.pipeline.runs")
    case = BY_NAME["clean-first-pass"]
    async with pipeline_env(pg, tmp_path, **_env_kwargs(case)) as env:
        run, status = await _run_case(env, case)
        assert "stages.missing_fact_check" not in status["g2"]["failing"]
        async with env.engine.begin() as conn:
            row = await runs.load_run(conn, run)
            done = await runs.load_steps(conn, run)
            tampered = [
                s if s.stage != "fact_check" else
                runs.StepRow(s.id, s.step, s.stage, s.input_sha256, s.output_sha256,
                             {**s.report, "notebook_sha256": "0" * 64}, s.reused_from, s.model, s.cost_usd,
                             s.duration_s)
                for s in done
            ]
            final = await env.notebook(done[-1].output_sha256)
            await steps_mod._finish(conn, env.runtime, row, tampered, final, done[-1].output_sha256)
        g2 = await env.scalar("SELECT g2 FROM compile_runs WHERE id = :r", r=run)
        assert "stages.missing_fact_check" in g2["failing"] and g2["publishable"] is False
