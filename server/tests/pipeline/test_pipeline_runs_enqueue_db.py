"""SRV-PIPELINE [db], paired SRV-OBS phase 2 change: a compile run's end enqueues `obs.runs` {compile, run id, end}
with `schedule_in` 60 s after its transaction commits; a failing enqueue never changes the run (INV-OBS-3).

@trace FR-PIPELINE-18 INV-OBS-3
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from obs.conftest import obs_setup

from .conftest import FIXTURES, Pg, load_yaml, pipeline_env, scripts_for

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
                   enabled: bool = True) -> tuple[Any, list[Any], str]:
    import reader.jobs

    captured = Captured(fail)
    monkeypatch.setattr(reader.jobs, "running", lambda: captured)
    case = CASES[name]
    with obs_setup("worker", enabled=enabled):
        async with pipeline_env(pg, tmp, labs=case.get("labs", "pass"), floors=RESERVES) as env:
            env.cli.scripts[:] = scripts_for(case["results"], case.get("labs", "pass"))
            await env.import_plan()
            run = await env.compile()
            for _ in range(6):
                await env.work()
            state = (await env.state(run))["state"]
    return run, [e for e in captured.enqueued if e[0] == "obs.runs"], str(state)


@pytest.mark.parametrize("name", ["clean-first-pass", "invalid-fact-report-exhausts-attempts"])
async def test_a_run_end_enqueues_obs_runs_once(pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                name: str) -> None:
    run, enqueued, _ = await _compile(pg, tmp_path, name, monkeypatch)
    assert enqueued == [("obs.runs", {"kind": "compile", "id": str(run), "end": 1}, 60)]


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
