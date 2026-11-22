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
    assert float(report["spent_usd"]) <= float(report["cap_usd"])
    assert abs(float(report["settled_usd"]) - float(report["sdk_usd"])) < 1e-6
