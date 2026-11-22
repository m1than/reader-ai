"""SRV-SEARCH [image]: the reader-embed image built and run for real, hardened and offline.

@trace FR-SEARCH-1 FR-SEARCH-2 INV-SEARCH-5

Opt-in with READER_TEST_IMAGE_BUILD=1 (OPS-DEPLOY FR-DEPLOY-2: the build downloads about 5 GB); needs the
daemon.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Iterator

import pytest

from .conftest import DOCKER, REPO

IMAGE = "reader-embed:local"
pytestmark = pytest.mark.skipif(
    not (DOCKER and os.environ.get("READER_TEST_IMAGE_BUILD") == "1"),
    reason="opt-in: READER_TEST_IMAGE_BUILD=1 and a docker daemon (builds reader-embed:local)",
)
TMPFS = "/tmp:size=64m,uid=10003"  # noqa: S108 - a container tmpfs, as compose.yaml declares it
HARDENING = [
    "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
    "--tmpfs", TMPFS, "--user", "10003:10003", "--memory", "3g", "--memory-swap", "3g",
]


def _docker(*args: str, stdin: str | None = None, timeout: float = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], input=stdin, capture_output=True, text=True, check=False, timeout=timeout
    )


@pytest.fixture(scope="module")
def built() -> str:
    dockerfile = str(REPO / "deploy" / "images" / "embed.Dockerfile")
    out = _docker("build", "-t", IMAGE, "-f", dockerfile, str(REPO), timeout=7200)
    assert out.returncode == 0, out.stderr[-3000:]
    return IMAGE


@pytest.fixture
def service(built: str) -> Iterator[str]:
    run = _docker("run", "-d", *HARDENING, built)
    assert run.returncode == 0, run.stderr
    cid = run.stdout.strip()
    try:
        for _ in range(240):
            check = _docker("exec", cid, "python", "-I", "-m", "reader.search.embed_service", "--healthcheck")
            if check.returncode == 0:
                break
            time.sleep(1)
        else:
            raise AssertionError(_docker("logs", cid).stderr[-2000:])
        yield cid
    finally:
        _docker("rm", "-f", cid)


CALL = """
import json, sys, urllib.request
req = urllib.request.Request("http://127.0.0.1:8100/embed", data=sys.stdin.read().encode(), method="POST",
                             headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=60) as r:
    body = json.loads(r.read())
print(json.dumps({"model": body["model"], "n": len(body["vectors"]), "dim": len(body["vectors"][0]),
                  "norm": sum(v * v for v in body["vectors"][0])}))
"""


def test_service_embeds_offline_under_the_hardening(service: str) -> None:
    out = _docker(
        "exec", "-i", service, "python", "-I", "-c", CALL,
        stdin=json.dumps({"kind": "query", "texts": ["как устроен блокчейн"]}),
    )
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got["model"] == "BAAI/bge-m3@5617a9f61b028005a4858fdac845db406aefb181"
    assert got["n"] == 1 and got["dim"] == 1024 and abs(got["norm"] - 1.0) < 1e-4
    user = _docker("exec", service, "id", "-u")
    assert user.stdout.strip() == "10003"


def test_logs_carry_no_text(service: str) -> None:
    canary = "kanareyka-embed-canary"
    body = json.dumps({"kind": "query", "texts": [canary]})
    _docker("exec", "-i", service, "python", "-I", "-c", CALL, stdin=body)
    logs = _docker("logs", service)
    assert canary not in logs.stdout + logs.stderr


EVIL = """
import os, pickle, sys
from reader.search.embed_service import load_checkpoint, UnsafeCheckpoint
class Evil:
    def __reduce__(self):
        return (os.system, ("true",))
path = "/tmp/evil.bin"
with open(path, "wb") as f:
    pickle.dump({"w": Evil()}, f)
try:
    load_checkpoint(path)
except UnsafeCheckpoint:
    print("refused")
"""


def test_checkpoint_loader_refuses_a_non_allow_listed_global(built: str) -> None:
    out = _docker("run", "--rm", *HARDENING, "--entrypoint", "python", built, "-I", "-c", EVIL)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == "refused"


def test_the_real_checkpoint_loads_with_weights_only(built: str) -> None:
    code = (
        "from reader.search.embed_service import load_checkpoint, MODEL_DIR; "
        "print(len(load_checkpoint(MODEL_DIR / 'pytorch_model.bin')))"
    )
    out = _docker("run", "--rm", *HARDENING, "--entrypoint", "python", built, "-I", "-c", code)
    assert out.returncode == 0, out.stderr[-2000:]
    assert int(out.stdout.strip()) > 100
