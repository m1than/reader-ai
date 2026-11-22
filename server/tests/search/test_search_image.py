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
# compose.yaml's embed hardening and limits (OPS-DEPLOY FR-DEPLOY-9), without a network
PROCESS = [
    "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--tmpfs", TMPFS,
    "--user", "10003:10003", "--memory", "3g", "--memory-swap", "3g", "--init",
]
HARDENING = ["--network", "none", *PROCESS]
HEALTH = ["--health-cmd", "python -I -m reader.search.embed_service --healthcheck", "--health-interval", "2s"]


def _docker(*args: str, stdin: str | None = None, timeout: float = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], input=stdin, capture_output=True, text=True, check=False, timeout=timeout
    )


def _python(
    image: str, code: str, stdin: str | None = None, network: str = "none"
) -> subprocess.CompletedProcess[str]:
    """A one-shot container of the image running `python -I -c code` (no docker exec)."""
    run = ["run", "--rm", "-i", "--network", network, *PROCESS]
    return _docker(*run, "--entrypoint", "python", image, "-I", "-c", code, stdin=stdin)


@pytest.fixture(scope="module")
def built() -> str:
    dockerfile = str(REPO / "deploy" / "images" / "embed.Dockerfile")
    out = _docker("build", "-t", IMAGE, "-f", dockerfile, str(REPO), timeout=7200)
    assert out.returncode == 0, out.stderr[-3000:]
    return IMAGE


@pytest.fixture
def service(built: str) -> Iterator[str]:
    """The real entrypoint, healthy by its own compose healthcheck command."""
    run = _docker("run", "-d", *HARDENING, *HEALTH, built)
    assert run.returncode == 0, run.stderr
    cid = run.stdout.strip()
    try:
        for _ in range(240):
            status = _docker("inspect", "-f", "{{.State.Health.Status}}", cid).stdout.strip()
            if status == "healthy":
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
    # a sidecar in the service's network namespace (which has no network but loopback)
    body = json.dumps({"kind": "query", "texts": ["как устроен блокчейн"]})
    out = _python(IMAGE, CALL, stdin=body, network=f"container:{service}")
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got["model"] == "BAAI/bge-m3@5617a9f61b028005a4858fdac845db406aefb181"
    assert got["n"] == 1 and got["dim"] == 1024 and abs(got["norm"] - 1.0) < 1e-4
    user = _docker("inspect", "-f", "{{.Config.User}}", service)
    assert user.stdout.strip() == "10003:10003"


def test_logs_carry_no_text(service: str) -> None:
    canary = "kanareyka-embed-canary"
    body = json.dumps({"kind": "query", "texts": [canary]})
    assert _python(IMAGE, CALL, stdin=body, network=f"container:{service}").returncode == 0
    logs = _docker("logs", service)
    assert "encoded kind=query n=1" in logs.stderr  # the service did log the call, as counts
    assert canary not in logs.stdout + logs.stderr


PRECHECK = """
from reader.search import embed_service as m
calls = []
original = m.load_checkpoint
def counted(path):
    calls.append(str(path))
    return original(path)
m.load_checkpoint = counted
encoder = m.load_encoder()
print(len(calls), calls[0].endswith("pytorch_model.bin"), encoder.model.max_seq_length)
"""


def test_service_start_runs_the_precheck_and_caps_the_length(built: str) -> None:
    out = _python(built, PRECHECK)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.split() == ["1", "True", "1024"]


# FR-SEARCH-1 / OPS-DEPLOY FR-DEPLOY-9: a real service start plus the worst admitted request (8 texts of 1,200
# characters that each reach the 1,024-token cap) under compose's 3g limit. The limit is sized on anonymous
# memory (memory.stat anon, sampled every 20 ms through the batch for its maximum): the mmap'd weights are
# file cache, which the kernel may reclaim at the limit, so memory.peak and memory.events max are reported only.
MEMORY = """
import json, random, threading, time, urllib.request
from reader.search import embed_service as m

def cgroup(name):
    with open("/sys/fs/cgroup/" + name) as f:
        return f.read()

def stat(key):
    return next(int(v) for k, v in (line.split() for line in cgroup("memory.stat").splitlines()) if k == key)

anon_max = [0]
sampling = [True]
def sample():
    while sampling[0]:
        anon_max[0] = max(anon_max[0], stat("anon"))
        time.sleep(0.02)
threading.Thread(target=sample, daemon=True).start()
server = m.make_server(None, "127.0.0.1", 0, m.model_id())
threading.Thread(target=server.serve_forever, daemon=True).start()
server.encoder = m.load_encoder()
after_load = stat("anon")
rng = random.Random(7)
pool = ("абвгдеёжзийклмнопрстуфхцчшщъыьэюя" + "αβγδεζηθλμξπσφψω0123456789+-*/=^_{}()[]<>|\\\\$%&#@~;:,.!?"
        + "".join(chr(c) for c in range(0x4E00, 0x4E00 + 200)))
texts = ["".join(rng.choice(pool) for _ in range(1200)) for _ in range(8)]
data = json.dumps({"kind": "passage", "texts": texts}).encode()
url = "http://127.0.0.1:%d/embed" % server.server_address[1]
request = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
with urllib.request.urlopen(request, timeout=600) as r:
    n = len(json.loads(r.read())["vectors"])
sampling[0] = False
events = dict(line.split() for line in cgroup("memory.events").splitlines())
tokens = [len(server.encoder.model.tokenizer(t)["input_ids"]) for t in texts]
print(json.dumps({"n": n, "anon_after_load": after_load, "anon_max": max(anon_max[0], stat("anon")),
                  "file": stat("file"), "peak": int(cgroup("memory.peak")),
                  "limit": int(cgroup("memory.max")), "events": events, "min_tokens": min(tokens)}))
"""


def test_worst_batch_stays_under_the_compose_limit(built: str) -> None:
    out = _python(built, MEMORY)
    assert out.returncode == 0, out.stderr[-2000:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    print(json.dumps(got))  # the measurement, recorded in the spec
    assert got["n"] == 8
    assert got["min_tokens"] > 1024  # every text reaches the cap: the worst admitted request
    assert got["limit"] == 3 * 1024**3
    assert got["events"]["oom_kill"] == "0"
    assert got["anon_max"] * 1.25 <= got["limit"]  # the spec's 25 % margin, on unreclaimable memory


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
