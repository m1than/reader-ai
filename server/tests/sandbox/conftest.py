"""Helpers for the OPS-SANDBOX [image] tests (spec/modules/OPS-SANDBOX.yaml).

The image `reader-kernel:local` is built once per session from sandbox/. Each test starts its own kernel
container with `docker run` flags equal to OPS-DEPLOY's `x-kernel` template (read from the source of
deploy/compose.yaml) plus `--restart always`, on a throwaway `--internal` network under the alias `kernel`.
"From inside the container" means `docker exec` as uid 1000 (the same uid, namespaces, capabilities and
seccomp as a cell) or a cell run through Kernel Gateway. "The caller" is a probe container on the same
network. Everything a test creates carries the label `org.reader-ai.test=sandbox` and is removed after the
test; a session finalizer removes leftovers.

Inputs that do not exist yet (sandbox/Dockerfile, the image) make a test fail on an assertion inside its
body.
"""

import json
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

REPO = Path(__file__).resolve().parents[3]
SANDBOX = REPO / "sandbox"
DOCKERFILE = SANDBOX / "Dockerfile"
LOCK = SANDBOX / "requirements.lock"
COMPOSE = REPO / "deploy" / "compose.yaml"
SERVER_DOCKERFILE = REPO / "deploy" / "images" / "server.Dockerfile"
PROBES = REPO / "spec" / "fixtures" / "OPS-SANDBOX" / "probes.yaml"
IMAGE = "reader-kernel:local"
LABEL = "org.reader-ai.test=sandbox"
NONCE = re.compile(r"^[0-9a-f]{32}$")
RESET_DEADLINE_S = 30.0  # SRV-KERNEL FR-KERNEL-5


def _daemon_up() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0  # noqa: S607


requires_docker_daemon = pytest.mark.skipif(not _daemon_up(), reason="docker daemon not reachable")


def docker(*args: str, timeout: float = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["docker", *args], capture_output=True, text=True, check=False, timeout=timeout,  # noqa: S607
    )


def load_yaml(path: Path) -> Any:
    assert path.exists(), f"{path} does not exist"
    return YAML(typ="safe").load(path.read_text(encoding="utf-8"))


def template_flags() -> list[str]:
    """`docker run` flags equal to OPS-DEPLOY's `x-kernel` template (source, not the rendered model)."""
    src = load_yaml(COMPOSE)["x-kernel"]
    flags = ["--user", str(src["user"])]
    if src.get("read_only"):
        flags.append("--read-only")
    for entry in src["tmpfs"]:
        flags += ["--tmpfs", str(entry)]
    for cap in src["cap_drop"]:
        flags += ["--cap-drop", str(cap)]
    for opt in src["security_opt"]:
        flags += ["--security-opt", str(opt)]
    flags += [
        "--memory", str(src["mem_limit"]), "--memory-swap", str(src["memswap_limit"]),
        "--cpus", str(src["cpus"]), "--pids-limit", str(src["pids_limit"]), "--restart", str(src["restart"]),
    ]
    return flags


@dataclass
class Build:
    ok: bool
    log: str


@pytest.fixture(scope="session")
def image() -> Iterator[Build]:
    if not DOCKERFILE.exists():
        yield Build(False, "sandbox/Dockerfile does not exist")
    else:
        proc = docker("build", "--platform", "linux/arm64", "-t", IMAGE, "-f", str(DOCKERFILE), str(SANDBOX),
                      timeout=1800)
        yield Build(proc.returncode == 0, (proc.stdout + proc.stderr)[-6000:])
    leftovers = docker("ps", "-aq", "--filter", f"label={LABEL}").stdout.split()
    if leftovers:
        docker("rm", "-f", *leftovers)
    for net in docker("network", "ls", "-q", "--filter", f"label={LABEL}").stdout.split():
        docker("network", "rm", net)


# A minimal Jupyter client run in a probe container: creates a kernel over REST (optionally with `env`),
# executes one cell over the kernel WebSocket and prints a JSON summary. Uses only what the kernel image
# ships (tornado).
CLIENT = r'''
import asyncio, json, sys, time, urllib.request, uuid
from tornado.websocket import websocket_connect
host, code, env, wait_s = sys.argv[1], sys.argv[2], json.loads(sys.argv[3]), float(sys.argv[4])
body = {"name": "python3"}
if env is not None:
    body["env"] = env
req = urllib.request.Request(f"http://{host}:8888/api/kernels", data=json.dumps(body).encode(),
                             headers={"Content-Type": "application/json"}, method="POST")
kid = json.load(urllib.request.urlopen(req, timeout=30))["id"]
out = {"kernel_id": kid, "stdout": "", "stderr": "", "error": None, "status": None, "states": [],
       "timeout": False}
async def main():
    ws = await websocket_connect(f"ws://{host}:8888/api/kernels/{kid}/channels")
    mid = uuid.uuid4().hex
    ws.write_message(json.dumps({"channel": "shell", "header": {"msg_id": mid, "msg_type": "execute_request",
        "session": "t", "username": "t", "version": "5.3"}, "parent_header": {}, "metadata": {},
        "content": {"code": code, "silent": False, "store_history": False, "user_expressions": {},
                    "allow_stdin": False, "stop_on_error": True}}))
    deadline = time.monotonic() + wait_s
    idle = False
    while True:
        try:
            raw = await asyncio.wait_for(ws.read_message(), max(0.1, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            out["timeout"] = True
            return
        if raw is None:
            out["closed"] = True
            return
        m = json.loads(raw)
        t = m["header"]["msg_type"]
        mine = m.get("parent_header", {}).get("msg_id") == mid
        if t == "status":
            st = m["content"]["execution_state"]
            out["states"].append(st)
            if st in ("restarting", "dead"):
                return
            idle = idle or (mine and st == "idle")
        elif mine and t == "stream":
            out[m["content"]["name"]] += m["content"]["text"]
        elif mine and t == "error":
            out["error"] = m["content"]["ename"]
        elif mine and t == "execute_reply":
            out["status"] = m["content"]["status"]
        if out["status"] is not None and idle:
            return
asyncio.run(main())
print(json.dumps(out))
'''

HTTP = r'''
import json, sys, urllib.request, urllib.error
method, url, body = sys.argv[1], sys.argv[2], sys.argv[3]
data = None if body == "-" else body.encode()
req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        print(json.dumps({"status": r.status, "body": r.read().decode(errors="replace")}))
except urllib.error.HTTPError as e:
    print(json.dumps({"status": e.code, "body": e.read().decode(errors="replace")}))
'''

STATE = r'''
import json, urllib.request
try:
    with urllib.request.urlopen("http://127.0.0.1:8889/control/state", timeout=2) as r:
        print(r.read().decode())
except Exception as e:
    print(json.dumps({"error": type(e).__name__}))
'''


@dataclass
class Sandbox:
    """One kernel container on its own internal network; cleaned up by the fixture."""

    build: Build
    tag: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    containers: list[str] = field(default_factory=list)
    network: str | None = None

    @property
    def name(self) -> str:
        return f"rk-{self.tag}"

    def _net(self) -> str:
        if self.network is None:
            net = f"rk-net-{self.tag}"
            proc = docker("network", "create", "--internal", "--label", LABEL, net)
            assert proc.returncode == 0, proc.stderr
            self.network = net
        return self.network

    def start(self, wait_up: bool = True) -> dict[str, Any]:
        assert self.build.ok, f"image reader-kernel:local did not build:\n{self.build.log}"
        net = self._net()
        proc = docker("run", "-d", "--name", self.name, "--label", LABEL, "--network", net,
                      "--network-alias", "kernel", *template_flags(), IMAGE)
        assert proc.returncode == 0, proc.stderr
        self.containers.append(self.name)
        return self.wait_state(lambda s: s.get("gateway") == "up") if wait_up else {}

    def sidecar(self, alias: str) -> None:
        """A stand-in service on the kernel's network (e.g. the caller `api`), listening on nothing."""
        name = f"rk-{alias}-{self.tag}"
        proc = docker("run", "-d", "--name", name, "--label", LABEL, "--network", self._net(),
                      "--network-alias", alias, "--entrypoint", "sleep", IMAGE, "600")
        assert proc.returncode == 0, proc.stderr
        self.containers.append(name)

    def exec(
        self, code: str, *, user: str = "1000:1000", timeout: float = 120,
    ) -> subprocess.CompletedProcess[str]:
        """Python code run inside the kernel container as `user` (a cell's uid by default)."""
        return docker("exec", "-u", user, self.name, "python3", "-I", "-c", code, timeout=timeout)

    def exec_ok(self, code: str, **kw: Any) -> str:
        proc = self.exec(code, **kw)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    def run(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return docker("exec", "-u", "1000:1000", self.name, *argv)

    def state(self) -> dict[str, Any]:
        proc = self.exec(STATE, timeout=30)
        if proc.returncode != 0 or not proc.stdout.strip():
            return {"error": "exec"}
        result: dict[str, Any] = json.loads(proc.stdout)
        return result

    def wait_state(self, cond: Any, timeout: float = 90) -> dict[str, Any]:
        end = time.monotonic() + timeout
        last: dict[str, Any] = {}
        while time.monotonic() < end:
            last = self.state()
            if cond(last):
                return last
            time.sleep(0.25)
        raise AssertionError(f"state condition not met within {timeout} s; last state {last}")

    def probe(self, code: str, *args: str, timeout: float = 180) -> subprocess.CompletedProcess[str]:
        """Python code run in a probe container on the kernel's network (the caller's position)."""
        return docker("run", "--rm", "--label", LABEL, "--network", self._net(), "--entrypoint", "python3",
                      IMAGE, "-I", "-c", code, *args, timeout=timeout)

    def http(self, method: str, port: int, path: str, body: str | None = None) -> dict[str, Any]:
        proc = self.probe(HTTP, method, f"http://kernel:{port}{path}", "-" if body is None else body)
        assert proc.returncode == 0, proc.stderr
        result: dict[str, Any] = json.loads(proc.stdout)
        return result

    def cell(self, code: str, *, env: dict[str, str] | None = None, wait_s: float = 120) -> dict[str, Any]:
        proc = self.probe(CLIENT, "kernel", code, json.dumps(env), str(wait_s), timeout=wait_s + 60)
        assert proc.returncode == 0, proc.stderr
        result: dict[str, Any] = json.loads(proc.stdout.strip().splitlines()[-1])
        return result

    def inspect(self) -> dict[str, Any]:
        proc = docker("inspect", self.name)
        assert proc.returncode == 0, proc.stderr
        result: dict[str, Any] = json.loads(proc.stdout)[0]
        return result

    def cleanup(self) -> None:
        if self.containers:
            docker("rm", "-f", *self.containers)
        if self.network:
            docker("network", "rm", self.network)


@pytest.fixture
def sandbox(image: Build) -> Iterator[Sandbox]:
    sb = Sandbox(image)
    yield sb
    sb.cleanup()
