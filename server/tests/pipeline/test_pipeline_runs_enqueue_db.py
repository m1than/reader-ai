"""SRV-PIPELINE [db], paired SRV-OBS phase 2 change: a compile run's end enqueues `obs.runs` {compile, run id, end}
with `schedule_in` 60 s after its transaction commits; a failing enqueue never changes the run (INV-OBS-3). Only
the step that ended the run counts an end; a resumed run ends again with end 2; a cancel counts its own end. The
`obs.runs` handler against the real ledger: its snapshot (FINISHED, FAILED at the last step's end, KILLED), two
concurrent ends serialised by the advisory lock, and no request with tracing off (SRV-OBS FR-OBS-20). The
ending-step rule case by case, and the label mirror's lookup for a run that stopped before finishing (FR-OBS-24).

@trace FR-PIPELINE-18 FR-OBS-20 FR-OBS-24 INV-OBS-3
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from obs.conftest import obs_setup

from .conftest import BY_NAME_HINT, FIXTURES, Pg, client, lab_modes, load_yaml, pipeline_env, scripts_for

pytestmark = pytest.mark.anyio
CASES = {c["name"]: c for c in load_yaml(FIXTURES / "loop-cases.yaml")["cases"]}
RESERVES = load_yaml(FIXTURES / "loop-cases.yaml")["defaults"]["reserves"]


class Captured:
    def __init__(self, fail: bool = False) -> None:
        self.fail, self.enqueued = fail, []

    async def enqueue(self, kind: str, args: Any, schedule_in: int = 0) -> Any:
        if self.fail and kind == "obs.runs":
            raise RuntimeError("queue down")
        self.enqueued.append((kind, dict(args), schedule_in))
        return None


async def _compile(pg: Pg, tmp: Path, name: str, monkeypatch: pytest.MonkeyPatch, *, fail: bool = False,
                   enabled: bool = True, inside: Any = None) -> tuple[Any, list[Any], str]:
    """One loop case to its end; `inside(env, run)`, when given, runs in the env after the end."""
    import reader.jobs

    captured = Captured(fail)
    monkeypatch.setattr(reader.jobs, "running", lambda: captured)
    case = CASES[name]
    with obs_setup("worker", enabled=enabled):
        async with pipeline_env(pg, tmp, labs=case.get("labs", "pass"), floors=RESERVES) as env:
            env.cli.scripts[:] = scripts_for(case["results"], case.get("labs", "pass"))
            env.verify.modes[:] = lab_modes(case["results"])
            await env.import_plan()
            run = await env.compile(str(case.get("cap", "8.00")))
            for _ in range(6):
                await env.work()
            state = (await env.state(run))["state"]
            if inside is not None:
                await inside(env, run)
    return run, [e for e in captured.enqueued if e[0] == "obs.runs"], str(state)


@pytest.mark.parametrize("name", [
    "clean-first-pass", "invalid-fact-report-exhausts-attempts", "one-loop-converges", "four-checks-fail-once",
    "loop-cap-before-first-loop", "budget-refused-mid-loop", "budget-stops-before-draft",
])  # ADR-0039 order: the findings step ends a run (or a refusal before any notebook)
async def test_a_run_end_enqueues_obs_runs_once(pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                name: str) -> None:
    """Only the step that ended the run counts (code review of 8466329: never one per stage)."""
    ends: list[int] = []

    async def inside(env: Any, run: Any) -> None:
        ends.append(int(await env.scalar("SELECT ends FROM compile_runs WHERE id = :r", r=run)))

    run, enqueued, state = await _compile(pg, tmp_path, name, monkeypatch, inside=inside)
    assert state == CASES[name]["expect_state"]
    assert enqueued == [("obs.runs", {"kind": "compile", "id": str(run), "end": 1}, 60)]
    assert ends == [1]


class FakeMlflow:
    """An async in-test MLflow: one Run store; counts requests in flight to observe the advisory lock."""

    def __init__(self) -> None:
        self.run: dict[str, Any] | None = None
        self.requests: list[tuple[str, Any]] = []
        self.active = self.peak = self.creates = 0

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.02)
            path = request.url.path
            body = json.loads(request.content) if request.content else None
            self.requests.append((path, body))
            if path.endswith("/runs/search"):
                return httpx.Response(200, json={"runs": [self.run] if self.run else []})
            if path.endswith("/runs/create"):
                self.creates += 1
                self.run = {"info": {"run_id": f"r{self.creates}"}, "data": {"params": []}}
                return httpx.Response(200, json={"run": self.run})
            return httpx.Response(200, json={})
        finally:
            self.active -= 1

    def factory(self, **_: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def bodies(self, suffix: str) -> list[Any]:
        return [b for p, b in self.requests if p.endswith(suffix)]


def _ms(at: datetime) -> int:
    return int(at.timestamp() * 1000)


async def _handle(env: Any, fake: FakeMlflow, monkeypatch: pytest.MonkeyPatch, run: Any, end: int) -> None:
    from reader.obs import runs

    monkeypatch.setattr(runs, "httpx", SimpleNamespace(AsyncClient=fake.factory))
    async with env.engine.begin() as conn:
        await runs.handle(runs.RunsArgs(kind="compile", id=str(run), end=end), conn, None)


async def test_the_handler_writes_a_finished_runs_snapshot_and_serialises_two_ends(
    pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeMlflow()
    got: dict[str, Any] = {}

    async def inside(env: Any, run: Any) -> None:
        from reader.obs import runs

        async with env.engine.connect() as conn:
            got["snap"] = await runs.snapshot(conn, runs.RunsArgs(kind="compile", id=str(run), end=1),
                                              datetime.now(UTC))
        got["finished_at"] = await env.scalar("SELECT finished_at FROM compile_runs WHERE id = :r", r=run)
        await asyncio.gather(_handle(env, fake, monkeypatch, run, 1), _handle(env, fake, monkeypatch, run, 2))

    run, _, _ = await _compile(pg, tmp_path, "clean-first-pass", monkeypatch, inside=inside)
    snap = got["snap"]
    assert (snap.status, snap.outcome, snap.key) == ("FINISHED", "finished", str(run))
    assert snap.end_ms == _ms(got["finished_at"])
    assert snap.metrics["cost_usd.total"] > 0 and snap.metrics["loops"] >= 0
    assert fake.peak == 1 and fake.creates == 1  # the advisory lock: two ends, one Run, never at once
    assert [u["status"] for u in fake.bodies("/runs/update")] == ["FINISHED", "FINISHED"]
    assert [s["key"] for s in fake.bodies("/runs/set-tag")] == ["reader.complete", "reader.complete"]


async def test_a_failed_run_ends_at_its_last_steps_end(pg: Pg, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    got: dict[str, Any] = {}

    async def inside(env: Any, run: Any) -> None:
        from reader.obs import runs

        async with env.engine.connect() as conn:
            got["snap"] = await runs.snapshot(conn, runs.RunsArgs(kind="compile", id=str(run), end=1),
                                              datetime.now(UTC))
        got["last"] = await env.scalar(
            "SELECT max(finished_at) FROM job_keys WHERE args->>'run_id' = :r", r=str(run))

    await _compile(pg, tmp_path, "invalid-fact-report-exhausts-attempts", monkeypatch, inside=inside)
    snap = got["snap"]
    assert (snap.status, snap.outcome) == ("FAILED", "failed")
    assert snap.end_ms == _ms(got["last"])  # its real end, not the time the job read it


async def test_the_ending_step_rule_case_by_case(pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On a finished run: an earlier step's succeeded hook and a dead step's count nothing; the final step's
    counts (the hook runs once per committed outcome, so a call is an end); on a cancelled run nothing counts."""
    from reader.pipeline import jobs as pipeline_jobs

    got: dict[str, Any] = {}

    async def inside(env: Any, run: Any) -> None:
        last = int(await env.scalar("SELECT max(step) FROM compile_steps WHERE run_id = :r", r=run))

        async def end(step: int, outcome: str, stage: str = "findings") -> int:
            payload = {"run_id": str(run), "step": step, "stage": stage}
            await pipeline_jobs.after_end(env.engine, payload, outcome)
            return int(await env.scalar("SELECT ends FROM compile_runs WHERE id = :r", r=run))

        got["earlier"] = await end(last - 1, "succeeded", "adversarial_review")
        got["dead"] = await end(last, "dead")
        # ADR-0039 revision: a stale model step at the last step number has no row of its own: nothing counts
        got["stale"] = await end(last, "succeeded", "fact_check")
        got["final"] = await end(last, "succeeded")
        from sqlalchemy import text

        async with env.engine.begin() as conn:
            await conn.execute(text("UPDATE compile_runs SET cancelled_at = now() WHERE id = :r"), {"r": run})
        got["cancelled"] = await end(last, "succeeded")

    _, enqueued, _ = await _compile(pg, tmp_path, "clean-first-pass", monkeypatch, inside=inside)
    assert got == {"earlier": 1, "dead": 1, "stale": 1, "final": 2, "cancelled": 2}
    assert [e[1]["end"] for e in enqueued] == [1, 2]


async def test_a_stopped_runs_revision_is_labelled_through_its_draft_step(
    pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-OBS-24 (c), code review round 2: a run that stopped before finishing has no final_sha256, yet its draft
    step produced the lesson's current revision; the mirror finds that step's own job."""
    from reader.obs import labels

    got: dict[str, Any] = {}

    async def inside(env: Any, run: Any) -> None:
        rev = await env.scalar("SELECT output_sha256 FROM compile_steps WHERE run_id = :r AND stage = 'draft'",
                               r=run)
        owner = await env.scalar("SELECT owner_id FROM compile_runs WHERE id = :r", r=run)
        lesson = await env.scalar("SELECT lesson_id FROM compile_runs WHERE id = :r", r=run)
        got["final"] = await env.scalar("SELECT final_sha256 FROM compile_runs WHERE id = :r", r=run)
        got["draft_job"] = await env.scalar(
            "SELECT job_id FROM job_keys WHERE kind = 'pipeline.stage' AND args->>'run_id' = :r"
            " AND args->>'stage' = 'draft'", r=str(run))
        async with env.engine.connect() as conn:
            row = SimpleNamespace(target="lesson", target_id=lesson, owner_id=owner, lesson_revision_sha256=rev)
            got["job"] = await labels.labelled_job(conn, row)
            other = SimpleNamespace(target="lesson", target_id=lesson, owner_id=owner,
                                    lesson_revision_sha256="0" * 64)
            got["hand_edited"] = await labels.labelled_job(conn, other)

    _, _, state = await _compile(pg, tmp_path, "invalid-fact-report-exhausts-attempts", monkeypatch,
                                 inside=inside)
    assert state == "stage_failed" and got["final"] is None
    assert got["job"] is not None and got["job"].job_id == got["draft_job"]
    assert got["hand_edited"] is None  # a revision no run produced has no trace


async def test_tracing_off_the_handler_makes_no_request(pg: Pg, tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeMlflow()

    async def inside(env: Any, run: Any) -> None:
        await _handle(env, fake, monkeypatch, run, 1)

    await _compile(pg, tmp_path, "clean-first-pass", monkeypatch, enabled=False, inside=inside)
    assert fake.requests == []


async def test_a_resumed_run_ends_again_with_end_2(pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import reader.jobs

    captured = Captured()
    monkeypatch.setattr(reader.jobs, "running", lambda: captured)
    with obs_setup("worker"):
        async with pipeline_env(pg, tmp_path, floors=BY_NAME_HINT) as env:
            lesson = await env.import_plan()
            bad = {"stage": "fact_check", "cost": 0.24, "invalid": "summary_only"}
            env.cli.scripts[:] = scripts_for([{"stage": "draft", "cost": 0.6}, bad, bad, bad], "pass")
            async with client(env) as c:
                run = (await c.post(f"/api/lessons/{lesson}/compile", json={})).json()["run_id"]
                for _ in range(4):
                    await env.work()
                assert (await c.get(f"/api/compiles/{run}")).json()["state"] == "stage_failed"
                env.cli.scripts[:] = scripts_for([
                    {"stage": "fact_check", "cost": 0.24, "unsupported": 0, "contradicted": 0},
                    {"stage": "adversarial_review", "cost": 0.5, "blocking": 0, "nonblocking": 0},
                ], "pass")
                assert (await c.post(f"/api/compiles/{run}/resume")).status_code == 202
                for _ in range(3):
                    await env.work()
                assert (await c.get(f"/api/compiles/{run}")).json()["state"] == "converged"
    ends = [e[1]["end"] for e in captured.enqueued if e[0] == "obs.runs"]
    assert ends == [1, 2]


async def test_a_cancel_counts_its_end_and_the_run_is_killed(pg: Pg, tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    import reader.jobs
    from reader.obs import runs

    captured = Captured()
    monkeypatch.setattr(reader.jobs, "running", lambda: captured)
    case = CASES["clean-first-pass"]
    with obs_setup("worker"):
        async with pipeline_env(pg, tmp_path, floors=RESERVES) as env:
            env.cli.scripts[:] = scripts_for(case["results"], "pass")
            await env.import_plan()
            api_queue = Captured()  # the api's queue (the test env's catalog has the pipeline kinds only)
            real_enqueue = env.jobs.enqueue

            async def enqueue(kind: str, args: Any, schedule_in: int = 0) -> Any:
                if kind == "obs.runs":
                    return await api_queue.enqueue(kind, args, schedule_in)
                return await real_enqueue(kind, args, schedule_in)

            monkeypatch.setattr(env.jobs, "enqueue", enqueue)
            async with client(env) as c:
                run = (await c.post(f"/api/lessons/{env.lesson}/compile", json={})).json()["run_id"]
                assert (await c.post(f"/api/compiles/{run}/cancel", json={})).status_code == 200
                for _ in range(2):
                    await env.work()  # the queued step sees the cancel and counts nothing
            ends = await env.scalar("SELECT ends FROM compile_runs WHERE id = :r", r=run)
            async with env.engine.connect() as conn:
                snap = await runs.snapshot(conn, runs.RunsArgs(kind="compile", id=run, end=1), datetime.now(UTC))
    assert api_queue.enqueued == [("obs.runs", {"kind": "compile", "id": run, "end": 1}, 60)]
    assert [e for e in captured.enqueued if e[0] == "obs.runs"] == []
    assert ends == 1
    assert (snap.status, snap.outcome) == ("KILLED", "cancelled")


async def test_tracing_off_enqueues_nothing(pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, enqueued, _ = await _compile(pg, tmp_path, "clean-first-pass", monkeypatch, enabled=False)
    assert enqueued == []


async def test_a_failing_enqueue_leaves_the_run_ended(pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                     caplog: Any) -> None:
    with caplog.at_level(logging.WARNING):
        _, enqueued, state = await _compile(pg, tmp_path, "clean-first-pass", monkeypatch, fail=True)
    assert enqueued == []
    assert state == CASES["clean-first-pass"]["expect_state"]  # the run's outcome is unchanged
    assert any("obs runs_enqueue_failed kind=compile" in r.getMessage() for r in caplog.records)
