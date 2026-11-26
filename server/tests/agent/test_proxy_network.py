# @trace FR-AGENT-3
"""[image: build; opt-in with READER_TEST_IMAGE_BUILD=1] agent-proxy's client identity over a real Docker network:
on a throwaway internal network with OPS-DEPLOY's agentnet subnet and fixed addresses, a container at the worker's
address is issued a run token and a container at another address gets 403 client_unknown. Skips when the subnet is
taken (the live stack's own agentnet)."""

from __future__ import annotations

import os
import subprocess
import uuid

import pytest

from .conftest import DOCKER, REPO

DOCKERFILE = REPO / "deploy" / "images" / "agent-proxy.Dockerfile"
pytestmark = pytest.mark.skipif(
    not DOCKER or os.environ.get("READER_TEST_IMAGE_BUILD") != "1",
    reason="image build; set READER_TEST_IMAGE_BUILD=1 (and a reachable docker daemon)",
)
ISSUE = (
    "import json, sys, urllib.request, urllib.error\n"
    "req = urllib.request.Request('http://10.230.7.2:8080/run-tokens', method='POST',"
    " data=json.dumps({'user': 'ivan', 'max_output_tokens': 10, 'model': 'claude-haiku-5-5'}).encode(),"
    " headers={'content-type': 'application/json'})\n"
    "try:\n"
    "    r = urllib.request.urlopen(req, timeout=5); print(r.status, r.read().decode())\n"
    "except urllib.error.HTTPError as e:\n"
    "    print(e.code, e.read().decode())\n"
)


def _docker(*args: str, timeout: float = 1200) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False, timeout=timeout)  # noqa: S603, S607


def test_worker_address_is_a_client_and_another_address_is_not() -> None:
    run_id = uuid.uuid4().hex[:8]
    image, net = f"reader-agent-proxy:test-{run_id}", f"rap-net-{run_id}"
    assert _docker("build", "-q", "-t", image, "-f", str(DOCKERFILE), str(REPO)).returncode == 0
    names: list[str] = []
    try:
        made = _docker("network", "create", "--internal", "--subnet", "10.230.7.0/24", "--ip-range",
                       "10.230.7.128/25", net)
        if made.returncode != 0:
            pytest.skip("10.230.7.0/24 is in use (the live stack's agentnet)")
        proxy = f"rap-proxy-{run_id}"
        names.append(proxy)
        started = _docker("run", "-d", "--name", proxy, "--network", net, "--ip", "10.230.7.2", "--read-only",
                          "--tmpfs", "/tmp", "--cap-drop", "ALL", image, "--bind", "10.230.7.2:8080")
        assert started.returncode == 0, started.stderr
        results = {}
        for label, ip in (("worker", "10.230.7.10"), ("other", "10.230.7.200")):
            name = f"rap-{label}-{run_id}"
            names.append(name)
            out = _docker("run", "--name", name, "--network", net, "--ip", ip, "--entrypoint", "python", image,
                          "-I", "-c", "import time; time.sleep(2)\n" + ISSUE)
            results[label] = out.stdout.strip()
        # The worker is a client: without a provider file its user gets 409 provider_missing (not 403).
        assert results["worker"].startswith("409"), results
        assert results["other"].startswith("403") and "client_unknown" in results["other"], results
    finally:
        for name in names:
            _docker("rm", "-f", name)
        _docker("network", "rm", net)
        _docker("rmi", image)
