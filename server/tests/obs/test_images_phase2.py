"""SRV-OBS [image] (opt-in, READER_TEST_IMAGE_BUILD=1), phase 2 against real MLflow 3.18.0 (reader-mlflow, built by
id) started with FR-OBS-27's flags and environment: the shipped worker code (from the server image) writes Runs
(FR-OBS-20), the setup and the version's LoggedModel (FR-OBS-21, -26) and probes (FR-OBS-32); every request is in VEC-OBS-5
`writes` (FR-OBS-34 item 5); the no-egress probe (FR-OBS-33); the opt-in burst (FR-OBS-35, READER_TEST_BURST=1).
Zero credit.

@trace FR-OBS-20 FR-OBS-21 FR-OBS-26 FR-OBS-27 FR-OBS-32 FR-OBS-33 FR-OBS-34 FR-OBS-35
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Iterator

import pytest

from . import test_images as ti
from .conftest import REPO
from .test_images import _backend, sh

pytestmark = ti.pytestmark
RUN = "10000000-0000-4000-8000-0000000000aa"
LESSON = "20000000-0000-4000-8000-0000000000bb"


@pytest.fixture(scope="module")
def mlflow_image() -> str:
    return ti.build_id("-f", str(REPO / "deploy/otel/mlflow.Dockerfile"), str(REPO))


@pytest.fixture(scope="module")
def server_image() -> str:
    return ti.build_id("-f", str(REPO / "deploy/images/server.Dockerfile"), str(REPO))


@pytest.fixture
def prefix() -> Iterator[str]:
    name = "reader-test-obsp2-" + secrets.token_hex(4)
    yield name
    for kind in ("ps", "network", "volume"):
        cmd = ["docker", "ps", "-aq"] if kind == "ps" else ["docker", kind, "ls", "-q"]
        ids = sh(*cmd, "--filter", f"name={name}", check=False).split()
        if ids:
            sh(*(["docker", "rm", "-f"] if kind == "ps" else ["docker", kind, "rm"]), *ids, check=False)


def _worker(prefix: str, image: str, script: str, timeout: float = 300) -> dict:  # type: ignore[type-arg]
    out = sh("docker", "run", "--rm", "--network", f"{prefix}-obs", "-e", "READER_MLFLOW_URL=http://mlflow:5000",
             "--entrypoint", "python", image, "-c", script, timeout=timeout)
    return json.loads(out.strip().splitlines()[-1])  # type: ignore[no-any-return]


SCRIPT = """
import asyncio, json, httpx
from reader.obs import bootstrap, runs, labels, bench, health, phase2
SETUP = phase2.SETUP
seen = []
async def log(request):
    seen.append([request.method, request.url.path])
async def main():
    async with httpx.AsyncClient(base_url="http://mlflow:5000", trust_env=False,
                                 event_hooks={"request": [log]}) as c:
        await bootstrap.ensure_setup(c, SETUP, version_key="ab" * 32, inputs={"prompt_set_sha256": "x"},
                                     day="2026-11-25")
        before = len(seen)
        await bootstrap.ensure_setup(c, SETUP, version_key="ab" * 32, inputs={"prompt_set_sha256": "x"},
                                     day="2026-11-25")
        repeat_writes = [p for p in seen[before:] if p[0] != "GET" and not p[1].endswith(("/search", "/get"))]
        snap = runs.Snapshot(kind="compile", key=%(run)r, name="compile " + %(run)r[:8], start_ms=1795000000000,
            end_ms=1795000160000, status="FINISHED", outcome="finished", end=1,
            tags={"reader.lesson.id": %(lesson)r}, params={"version_key": "abababababab", "reader_version": "0.1.0"},
            metrics={"cost_usd.total": 1.84}, snapshot_at="2026-11-25T12:00:00Z")
        await runs.sync(snap, c)
        await runs.sync(snap, c)  # idempotent: one Run
        found = (await c.post("/api/2.0/mlflow/runs/search", json={"experiment_ids": ["0"],
                 "filter": "tags.`reader.run_id` = '%(run)s'"})).json().get("runs", [])
        probe = await health.probe(c)
        exps = (await c.get("/api/2.0/mlflow/experiments/get", params={"experiment_id": "0"})).json()
    return {"repeat_writes": repeat_writes, "runs": found, "probe": {k: list(v) for k, v in probe.items()},
            "exp0": exps["experiment"]["name"], "paths": seen,
            "declared": [[w["method"], w["path"]] for w in phase2.VEC["writes"]]}
print(json.dumps(asyncio.run(main())))
"""


def test_phase2_writes_against_real_mlflow(prefix: str, mlflow_image: str, server_image: str) -> None:
    _backend(prefix, mlflow_image)
    got = _worker(prefix, server_image, SCRIPT % {"run": RUN, "lesson": LESSON})
    assert got["exp0"] == "reader"  # FR-OBS-26
    assert got["repeat_writes"] == []  # 0 writes on a repeat the same day
    (run,) = got["runs"]  # FR-OBS-20: one Run for two syncs
    tags = {t["key"]: t["value"] for t in run["data"]["tags"]}
    assert tags["reader.complete"] == "true" and tags["reader.snapshot_at"] == "2026-11-25T12:00:00Z"
    assert got["probe"]["mlflow"] == [True, "healthy"]
    templated = [tuple(p) for p in got["declared"]]
    for method, path in got["paths"]:  # FR-OBS-34 (5): every request is a declared write or read
        assert any(m == method and _matches(t, path) for m, t in templated), (method, path)


def _matches(template: str, path: str) -> bool:
    import re

    return re.fullmatch(re.sub(r"\\\{[a-z_]+\\\}", "[^/]+", re.escape(template)), path) is not None


def test_server_runs_with_the_phase2_flags(prefix: str, mlflow_image: str, server_image: str) -> None:
    """FR-OBS-27: under compose's command (test_images._backend must run it), the gateway, assistant and jobs routes
    404 and /health carries X-Frame-Options DENY. WAL (FR-OBS-28) is pinned in test_images_migrate.py."""
    _backend(prefix, mlflow_image)
    got = _worker(prefix, server_image, """
import json, httpx
c = httpx.Client(base_url="http://mlflow:5000", trust_env=False)
h = c.get("/health")
print(json.dumps({"xfo": h.headers.get("x-frame-options"),
  "gateway": c.get("/ajax-api/3.0/mlflow/gateway/endpoints").status_code,
  "assistant": c.get("/ajax-api/3.0/mlflow/assistant/status").status_code,
  "jobs": c.get("/ajax-api/3.0/jobs").status_code}))
""")
    assert got == {"xfo": "DENY", "gateway": 404, "assistant": 404, "jobs": 404}
    compose = (REPO / "deploy" / "compose.yaml").read_text(encoding="utf-8")
    assert "--x-frame-options" in compose and "--serve-artifacts" in compose  # the backend runs compose's command


def test_no_egress_from_mlflows_networks(prefix: str, mlflow_image: str, server_image: str) -> None:
    """FR-OBS-33 (round 11/12): on mlflow's networks an IP connect fails and a name does not resolve, /health is 200;
    the same probe on a normal network connects (the positive control)."""
    _backend(prefix, mlflow_image)
    probe = """
import json, socket, urllib.request
def connect():
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=3).close(); return True
    except OSError:
        return False
def resolve():
    try:
        socket.getaddrinfo("example.com", 443); return True
    except OSError:
        return False
def health():
    try:
        return urllib.request.urlopen("http://mlflow:5000/health", timeout=3).status
    except Exception:
        return None
print(json.dumps({"connect": connect(), "resolve": resolve(), "health": health()}))
"""
    inside = _worker(prefix, server_image, probe)
    assert inside == {"connect": False, "resolve": False, "health": 200}
    outside = json.loads(sh("docker", "run", "--rm", "--entrypoint", "python", server_image, "-c", probe).strip())
    assert outside["connect"] is True and outside["resolve"] is True


@pytest.mark.skipif(os.environ.get("READER_TEST_BURST") != "1", reason="burst test; set READER_TEST_BURST=1")
def test_burst_limits(prefix: str, mlflow_image: str, server_image: str) -> None:
    """FR-OBS-35: 60 lessons x 8 jobs x 72 spans plus 100 tutor questions; 0 dropped, p95 <= 30 s, peaks <= 80 %."""
    _backend(prefix, mlflow_image)
    got = _worker(prefix, server_image, "import json\nfrom reader.obs import burst\n"
                  "print(json.dumps(burst.run('http://rotel:4318', 'http://mlflow:5000')))", timeout=900)
    assert got["sent"] == got["stored"] == 60 * 8 * 72 + 100 * 10
    assert got["p95_s"] <= 30
