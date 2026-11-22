# @trace FR-AGENT-1
"""The agent-proxy image: pinned base, allow-listed inputs, uid 10002, entry point, delivery tools and the health
command. Static checks always; [image] build and health run with Docker."""

from __future__ import annotations

import subprocess
import time
import uuid

import pytest

from .conftest import DOCKER, REPO

DOCKERFILE = REPO / "deploy" / "images" / "agent-proxy.Dockerfile"
SERVER_DOCKERFILE = REPO / "deploy" / "images" / "server.Dockerfile"
IMAGE = "reader-agent-proxy:local"


def _lines(kind: str) -> list[str]:
    assert DOCKERFILE.exists(), "deploy/images/agent-proxy.Dockerfile does not exist yet"
    return [
        ln.strip() for ln in DOCKERFILE.read_text().splitlines() if ln.strip().upper().startswith(kind + " ")
    ]


def test_dockerfile_static() -> None:
    base = _lines("FROM")
    server_base = [ln for ln in SERVER_DOCKERFILE.read_text().splitlines() if ln.startswith("FROM ")]
    assert base == server_base  # the same pinned python:3.14-slim-trixie digest
    copies = {part for ln in _lines("COPY") for part in ln.split()[1:-1] if not part.startswith("--")}
    assert copies == {"server/pyproject.toml", "server/src"}
    assert _lines("ADD") == []
    assert _lines("USER") == ["USER 10002:10002"]
    assert _lines("ENTRYPOINT") == ['ENTRYPOINT ["python", "-I", "-m", "reader.agent.proxy"]']
    text = DOCKERFILE.read_text()
    assert "[agent]" not in text  # the SDK extra is not installed in agent-proxy
    assert "/usr/local/bin/reader-agent-proxy-health" in text
    assert (REPO / "deploy" / "images" / "agent-proxy.Dockerfile.dockerignore").exists()


def _docker(*args: str, timeout: float = 1200) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False, timeout=timeout)


@pytest.mark.skipif(not DOCKER, reason="docker daemon not reachable")
def test_image_builds_and_reports_health() -> None:
    build = _docker("build", "-q", "-t", IMAGE, "-f", str(DOCKERFILE), str(REPO))
    assert build.returncode == 0, build.stderr[-2000:]
    for tool in ("sh", "cat", "mv"):
        probe = _docker("run", "--rm", "--entrypoint", "sh", IMAGE, "-c", f"command -v {tool}")
        assert probe.returncode == 0, tool
    probe = _docker("run", "--rm", "--entrypoint", "id", IMAGE, "-u")
    assert probe.stdout.strip() == "10002"
    name = "rap-" + uuid.uuid4().hex[:8]
    # No reader.yaml mounted: the proxy still listens (zero provider users is healthy-idle).
    run = _docker("run", "-d", "--name", name, "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL", IMAGE)
    assert run.returncode == 0, run.stderr
    try:
        healthy = False
        for _ in range(40):
            if _docker("exec", name, "/usr/local/bin/reader-agent-proxy-health").returncode == 0:
                healthy = True
                break
            time.sleep(0.5)
        assert healthy, _docker("logs", name).stderr[-2000:]
    finally:
        _docker("rm", "-f", name)
