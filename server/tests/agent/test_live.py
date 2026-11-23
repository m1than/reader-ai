# @trace FR-AGENT-11, INV-AGENT-1
"""[live] against the running stack (`reader up` with profile `agents`). Skipped unless READER_LIVE=1: FR-AGENT-11
spends about $0.03 of the owner's test budget. The checks never read a token: they look for the absence of token
files, variables and CLI state in the worker and kernel containers."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("READER_LIVE") != "1", reason="live check: READER_LIVE=1")


def _container(service: str) -> str:
    out = subprocess.run(
        ["docker", "ps", "-q", "--filter", f"label=com.docker.compose.service={service}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert out, f"{service} is not running"
    return out[0]


def _exec(service: str, *cmd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "exec", _container(service), *cmd],
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )


def test_one_sonnet_call_through_agent_proxy_lands_in_the_ledger() -> None:
    result = _exec("worker", "python", "-I", "-m", "reader.agent.live")
    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(result.stdout)
    assert report["model"] == "claude-sonnet-5-5"
    assert report["answered"] is True
    assert report["kinds"] == ["reserve", "settle"]
    assert 0 < report["settled_usd"] < 0.2


@pytest.mark.parametrize("service", ["worker", "kernel-verify", "kernel-agent"])
def test_no_provider_token_outside_agent_proxy(service: str) -> None:
    env = _exec(service, "sh", "-c", "env | cut -d= -f1")
    names = set(env.stdout.split())
    assert not names & {"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}
    environs = _exec(
        service, "sh", "-c", "cat /proc/[0-9]*/environ 2>/dev/null | tr '\\0' '\\n' | cut -d= -f1"
    )
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in environs.stdout.split()
    secrets_dir = _exec(service, "sh", "-c", "ls -A /run/secrets 2>/dev/null")
    assert all(name == "db-password" for name in secrets_dir.stdout.split())
    claude_dir = _exec(service, "sh", "-c", "ls -A ~/.claude 2>/dev/null")
    assert claude_dir.stdout.strip() == ""
