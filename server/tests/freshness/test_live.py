# @trace FR-FRESH-14
"""[live] against the running stack (`reader up` with profile `agents`, the pilot imported, an owner-entered
bibliography). Skipped unless READER_LIVE=1: one check spends about $0.27 (at most $1.00, the run cap) of
budget.test_budget_usd. `reader.freshness.live` runs in the worker container, starts one check the way the
POST route does, runs it to a final status and prints ids, counts, flags, costs and per search the gate's
decision and input tokens only: no query, title, URL, bibliography field or model text leaves the
container."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("READER_LIVE") != "1", reason="live check: READER_LIVE=1")


def _worker() -> str:
    out = subprocess.run(
        ["docker", "ps", "-q", "--filter", "label=com.docker.compose.service=worker"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert out, "worker is not running"
    return out[0]


def test_one_check_completes_within_the_caps() -> None:
    result = subprocess.run(  # noqa: S603
        ["docker", "exec", _worker(), "python", "-I", "-m", "reader.freshness.live"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=3300,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(result.stdout)
    assert report["status"] == "done"
    assert report["verdict"] in ("current", "partly_outdated", "outdated")
    assert report["searches_with_results"] >= 1
    assert report["stored_urls_in_r"] is True
    assert 0 < report["usd"] <= 1.00
    assert report["ledger_rows_with_web_search_requests"] >= 1
    assert report["crosscheck_ok"] is True
    for search in report["searches"]:
        assert set(search) == {"gate", "nested_input_tokens", "status"}
