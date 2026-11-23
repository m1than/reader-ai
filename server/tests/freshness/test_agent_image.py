# @trace INV-FRESH-1 FR-FRESH-6 FR-FRESH-7
"""[agent] INV-FRESH-1 end to end through the real adapter, the real bundled CLI (reader-server:local) and a
real agent-proxy (`create_app`), against the handshake spike's stub in its `websearch` scenario, with canary
spans: `--network none`, no token, no credit. agent_check.py runs inside the image; the ledger's reserve and
settle are replaced in memory there (no database in the container); everything else is the production path:
`scan.parse`, `queries.plan`, `jobs.search_call`, `Gate`, `AgentProvider.run` with `sdk_cli`, and the
proxy's checks.

Asserted for every query `plan` returns and every body agent-proxy forwards to the stub: no 6-word run of a
canary sentence and at most 5 canary words; and for every run: the PreToolUse matcher is `WebSearch`, its one
hook is the caller's Gate, the gate allowed the planned query once, and the run's R came from the stub."""

from __future__ import annotations

import json
import secrets
import shutil
import subprocess
from decimal import Decimal
from typing import Any

import pytest

from .conftest import REPO, bounds

IMAGE = "reader-server:local"
STUB = REPO / "spikes" / "agent-proxy-handshake" / "stub.py"
CHECK = REPO / "server" / "tests" / "freshness" / "agent_check.py"


def _docker_up() -> bool:
    if shutil.which("docker") is None:
        return False
    found = subprocess.run(["docker", "info"], capture_output=True, check=False, timeout=20)  # noqa: S607
    return found.returncode == 0


pytestmark = pytest.mark.skipif(not _docker_up(), reason="docker daemon not reachable")


def _docker(*args: str, timeout: float = 1800) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["docker", *args], capture_output=True, text=True, check=False, timeout=timeout  # noqa: S607
    )


@pytest.fixture(scope="module")
def report(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    build = _docker(
        "build", "-q", "-t", IMAGE, "-f", str(REPO / "deploy" / "images" / "server.Dockerfile"), str(REPO)
    )
    assert build.returncode == 0, build.stderr[-2000:]
    out = tmp_path_factory.mktemp("agent")
    out.chmod(0o777)
    handle = secrets.token_urlsafe(32)
    flags = [
        "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp", "--tmpfs", "/home/reader",  # noqa: S108
        "-v", f"{STUB}:/stub/stub.py:ro", "-v", f"{CHECK}:/check/agent_check.py:ro", "-v", f"{out}:/out",
        "-e", f"EXPECT_HANDLE={handle}", "--entrypoint", "python",
    ]
    result = _docker("run", *flags, IMAGE, "/check/agent_check.py", timeout=900)
    assert result.returncode == 0, result.stderr[-3000:]
    data: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    return data


def _canary(tokens: list[str], data: dict[str, Any]) -> tuple[int, bool]:
    canary = set(data["canary_words"])
    runs = {tuple(r) for r in data["sentence_6runs"]}
    copied = any(tuple(tokens[i : i + 6]) in runs for i in range(len(tokens) - 5))
    return sum(1 for t in tokens if t in canary), copied


def test_planned_queries_hold_no_canary_run(report: dict[str, Any]) -> None:
    from reader.freshness.text import words

    assert report["scan_dropped"] == {"subject_too_long": 1}  # the punctuation-joined 6-word subject
    assert report["plan_dropped"] == {"query_unsafe": 1}  # the 7-word copy with a topic word inside
    assert [t for t, _ in report["queries"]] == ["edition", "claim", "claim", "books"]
    counts = []
    for _, text in report["queries"]:
        count, copied = _canary(words(text), report)
        assert count <= bounds()["subject_words_max"]
        assert not copied
        counts.append(count)
    assert max(counts) == bounds()["subject_words_max"]  # not vacuous: a query carries 5 canary words


def test_every_forwarded_body_is_bounded(report: dict[str, Any]) -> None:
    assert len(report["runs"]) == len(report["queries"])
    seen_five = False
    for run in report["runs"]:
        kinds = [b["kind"] for b in run["bodies"]]
        assert kinds == ["main", "search", "main"], run  # two main-loop requests and the nested search
        for body in run["bodies"]:
            assert len(body["canary_words"]) <= bounds()["subject_words_max"]
            assert body["canary_6run"] is False
            seen_five |= len(body["canary_words"]) == bounds()["subject_words_max"]
    assert seen_five  # the 5-word subject reached the stub, as INV-FRESH-1 (a) allows


def test_the_hook_is_the_callers_gate_on_websearch(report: dict[str, Any]) -> None:
    for run in report["runs"]:
        assert run["matchers"] == ["WebSearch"]
        assert run["hook_is_the_gate"] is True
        # the stub echoed the planned query; the gate allowed it once and denied nothing
        assert (run["allowed"], run["denied"]) == (1, 0)


def test_each_run_completes_with_r_from_the_stub(report: dict[str, Any]) -> None:
    for run in report["runs"]:
        assert run["outcome"] == "ok", run
        assert run["r"] == 2  # the stub's two synthetic results, from UserMessage.tool_use_result
        assert run["web_search_requests"] == 1
    floor = Decimal(str(bounds()["search_reserve_floor_usd"]))
    assert [Decimal(r) for r in report["reserves"]] == [floor] * len(report["runs"])  # FR-FRESH-7's floor
