# @trace INV-SANDBOX-7
"""[live] against the running stack (`reader up` with `agents`), skipped unless READER_LIVE=1: the worker
services' credential and route checks (ADR-0025 `verified_by` as amended by ADR-0039 B7b). Each probe runs
inside `worker` or `worker-judge` as uid 10001 through `docker exec`; it reads no token: it sends no bearer,
a random one, or a run token it issued and revoked itself."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("READER_LIVE") != "1", reason="live check: READER_LIVE=1")
LANES = ("worker", "worker-judge")
PROXY = "http://agent-proxy:8080"

BEARERS = r"""
import json, secrets, urllib.request, urllib.error

def post(path, body, bearer=None):
    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
    if bearer is not None:
        headers["authorization"] = "Bearer " + bearer
    req = urllib.request.Request(PROXY + path, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")

message = {"model": "claude-haiku-5-5", "max_tokens": 8, "messages": [{"role": "user", "content": "x"}]}
out = {}
out["none"] = post("/v1/messages?beta=true", message)
out["random"] = post("/v1/messages?beta=true", message, secrets.token_urlsafe(32))
status, issued = post("/run-tokens", {"user": USER, "max_output_tokens": 8, "model": "claude-haiku-5-5"})
if status == 200:
    post("/run-tokens/revoke", {"run_token": issued["run_token"]})
    out["revoked"] = post("/v1/messages?beta=true", message, issued["run_token"])
else:
    out["revoked"] = (None, {"issue": status})
print(json.dumps(out))
"""

RESOLVE = r"""
import json, socket
names = NAMES
out = {}
for name in names:
    try:
        socket.getaddrinfo(name, 80)
        out[name] = True
    except socket.gaierror:
        out[name] = False
print(json.dumps(out))
"""

SCAN = r"""
import json, socket, concurrent.futures
addrs = sorted({ai[4][0] for ai in socket.getaddrinfo(TARGET, 80, socket.AF_INET)})
def probe(arg):
    addr, port = arg
    s = socket.socket(); s.settimeout(0.5)
    try:
        return s.connect_ex((addr, port)) == 0
    finally:
        s.close()
with concurrent.futures.ThreadPoolExecutor(256) as pool:
    open_ = [a for a, ok in zip([(a, p) for a in addrs for p in range(1, 65536)],
             pool.map(probe, [(a, p) for a in addrs for p in range(1, 65536)])) if ok]
print(json.dumps({"addrs": addrs, "open": open_}))
"""


def _container(service: str) -> str:
    out = subprocess.run(  # noqa: S603
        ["docker", "ps", "-q", "--filter", f"label=com.docker.compose.service={service}"],  # noqa: S607
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert out, f"{service} is not running"
    return out[0]


def _python(service: str, code: str, timeout: float = 900) -> object:
    done = subprocess.run(  # noqa: S603
        ["docker", "exec", "-u", "10001", _container(service), "python", "-I", "-c", code],  # noqa: S607
        capture_output=True, text=True, check=False, timeout=timeout,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout)


@pytest.mark.parametrize("service", LANES)
def test_no_valid_handle_gets_no_credential(service: str) -> None:
    user = os.environ.get("READER_LIVE_USER", "ivan")
    code = f"PROXY = {PROXY!r}\nUSER = {user!r}\n" + BEARERS
    out = _python(service, code)
    assert isinstance(out, dict)
    for case in ("none", "random", "revoked"):
        status, body = out[case]
        assert (status, body) == (401, {"error": "run_token_unknown"}), (service, case, out)


def test_worker_judge_resolves_only_its_routes() -> None:
    reachable = ["agent-proxy", "postgres"]
    unreachable = [
        "mcp", "embed", "caddy", "mlflow", "jaeger", "kernel-verify", "kernel-agent", "kernel-pair-1"
    ]
    out = _python("worker-judge", f"NAMES = {reachable + unreachable!r}\n" + RESOLVE)
    assert isinstance(out, dict)
    assert all(out[n] for n in reachable), out
    assert not any(out[n] for n in unreachable), out


@pytest.mark.parametrize(("source", "target"), [("worker", "worker-judge"), ("worker-judge", "worker")])
def test_neither_worker_service_listens_for_the_other(source: str, target: str) -> None:
    out = _python(source, f"TARGET = {target!r}\n" + SCAN)
    assert isinstance(out, dict)
    assert out["addrs"], out
    assert out["open"] == [], out
