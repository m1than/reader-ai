# @trace FR-TUTOR-14
"""[live] against the running stack (`reader up` with profile `agents`, L02 compiled, its material embedded).
Skipped unless READER_LIVE=1: one question spends at most question_cap_usd ($0.30) of budget.test_budget_usd.
`reader.tutor.live` runs in the worker container, asks about the first explain cell of the owner's first lesson
whose course has a material, waits for a terminal state and prints ids, counts, flags, timings and costs only:
no question, quote or answer text leaves the container."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("READER_LIVE") != "1", reason="live check: READER_LIVE=1")


def _worker() -> str:
    out = subprocess.run(
        ["docker", "ps", "-q", "--filter", "label=com.docker.compose.service=worker"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert out, "worker is not running"
    return out[0]


def test_one_question_is_answered_with_a_verified_citation_within_the_caps() -> None:
    result = subprocess.run(
        ["docker", "exec", _worker(), "python", "-I", "-m", "reader.tutor.live"],
        capture_output=True,
        text=True,
        check=False,
        timeout=700,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(result.stdout)
    assert set(report) == {"state", "grounded", "citations", "verified_blocks", "blocks", "usd", "seconds"}
    assert report["state"] == "answered"
    assert report["citations"] >= 1
    assert report["verified_blocks"] >= 1
    assert report["seconds"] <= 630
    assert 0 < report["usd"] <= 0.30
