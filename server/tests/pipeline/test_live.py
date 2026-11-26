# @trace FR-PIPELINE-13
"""[live] A real compile of L02 on the running stack. Skipped unless READER_LIVE_COMPILE=1: it spends credit from
the owner's $30 test budget (coordinator-scheduled). Reads the run through the worker container; never prints a
secret."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("READER_LIVE_COMPILE") != "1", reason="spends credit")


def test_l02_compile_ends_within_the_cap_without_task_budget_refusals() -> None:
    container = subprocess.run(
        ["docker", "ps", "-q", "--filter", "label=com.docker.compose.service=worker"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[0]
    result = subprocess.run(
        ["docker", "exec", container, "python", "-I", "-m", "reader.pipeline.live", "L02"],
        capture_output=True,
        text=True,
        check=False,
        timeout=3600,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(result.stdout)
    assert report["state"] in ("converged", "open_findings", "budget_exhausted")
    assert "task_budget_exhausted" not in report["refusals"]
    # FR-PIPELINE-13 revised (ADR-0039): up to 3 loops, a recorded stop, the effective budgets of the compile
    # tasks equal the run cap (D6), and the loop plans with their judge part are reported for the owner
    assert report["loops"] <= 3 and report["stop"] in ("passed", "loop_limit", "loop_cap", "budget_refused")
    for task in ("draft", "fact_check", "review"):
        assert float(report["effective_task_budgets"][task]) == float(report["cap_usd"]), task
    assert all("planned_usd" in p and "planned_judge_usd" in p for p in report["loop_plans"])
    assert isinstance(report["wall_s"], float)
    assert float(report["spent_usd"]) <= float(report["cap_usd"])
    assert abs(float(report["settled_usd"]) - float(report["sdk_usd"])) < 1e-6
