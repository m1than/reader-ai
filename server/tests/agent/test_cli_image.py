# @trace FR-AGENT-6, FR-AGENT-13
"""[image] The adapter's CLI path with the real bundled CLI in reader-server:local, against the handshake spike's
stub on 127.0.0.1 (--network none, no token, no credit): a 300,000-character system prompt starts (code review
B1: the argv limit is 131,071 bytes, so the prompt travels as a file), and a deadline kills the CLI at once
(S6: no 5 s close grace)."""

from __future__ import annotations

import json
import secrets
import subprocess
from pathlib import Path
from typing import Any

import pytest

from .conftest import DOCKER, REPO

IMAGE = "reader-server:local"
STUB = REPO / "spikes" / "agent-proxy-handshake" / "stub.py"
CHECK = Path(__file__).resolve().parent / "cli_check.py"
pytestmark = pytest.mark.skipif(not DOCKER, reason="docker daemon not reachable")


def _docker(*args: str, timeout: float = 1800) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False, timeout=timeout)


@pytest.fixture(scope="module")
def image() -> str:
    build = _docker(
        "build", "-q", "-t", IMAGE, "-f", str(REPO / "deploy" / "images" / "server.Dockerfile"), str(REPO)
    )
    assert build.returncode == 0, build.stderr[-2000:]
    return IMAGE


def _run(image: str, tmp: Path, mode: str, **env: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    handle = secrets.token_urlsafe(32)
    out = tmp / mode
    out.mkdir()
    out.chmod(0o777)
    flags = [
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/home/reader",
        "-v",
        f"{STUB}:/stub/stub.py:ro",
        "-v",
        f"{CHECK}:/check/cli_check.py:ro",
        "-v",
        f"{out}:/out",
        "-e",
        f"HANDLE={handle}",
        "-e",
        f"EXPECT_HANDLE={handle}",
        "--entrypoint",
        "python",
    ]
    for key, value in env.items():
        flags += ["-e", f"{key}={value}"]
    result = _docker("run", *flags, image, "/check/cli_check.py", mode, timeout=300)
    assert result.returncode == 0, result.stderr[-3000:]
    log = out / "requests.jsonl"
    requests = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return json.loads(result.stdout.strip().splitlines()[-1]), requests


def test_system_prompt_at_the_source_bound_starts_the_cli(image: str, tmp_path: Path) -> None:
    report, requests = _run(image, tmp_path, "big")
    assert report["ok"] is True, report
    assert report["init_tools"] == []
    messages = [r for r in requests if r.get("path") == "/v1/messages"]
    assert len(messages) == 1
    assert messages[0]["body_bytes"] > 300_000  # the whole prompt reached the API
    assert messages[0]["credential_is_handle"] is True


def test_deadline_kills_the_cli_at_once(image: str, tmp_path: Path) -> None:
    report, _ = _run(image, tmp_path, "deadline", STUB_DELAY_S="30")
    assert report.get("timeout") is True, report
    assert report["elapsed_s"] < 3.5  # deadline 2 s; the SDK's own close would add a 5 s grace
    assert report["cli_processes_left"] == 0


def test_abort_on_first_api_retry_kills_the_cli_at_once(image: str, tmp_path: Path) -> None:
    """The stub answers 429: the first api_retry ends the call and the CLI is killed, not waited for (S6)."""
    report, requests = _run(image, tmp_path, "abort", STUB_ERROR_STATUS="429")
    assert report["retried"] is True, report
    assert report["elapsed_s"] < 3.0  # the CLI's own backoff runs ~176 s; the SDK's close adds a 5 s grace
    assert report["cli_processes_left"] == 0
    assert len([r for r in requests if r.get("path") == "/v1/messages"]) <= 2
