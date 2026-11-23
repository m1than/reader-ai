# @trace FR-PIPELINE-9
"""Lab verification: the time bound (pure) and [image] runs of F01's lab against the kernel image (built under a
per-run tag, never the live stack's `reader-kernel:local`) with the x-kernel flags, its ports published on
127.0.0.1 (as SRV-KERNEL's image test does)."""

from __future__ import annotations

import copy
import json
import os
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from .conftest import DOCKER, F01, REPO, need, spec_bounds

pytestmark = pytest.mark.anyio
# A per-run tag (OPS-SANDBOX header, test image): never `reader-kernel:local`, the live stack's tag. Removed at the
# end of the module; the sandbox session-start sweep untags it if this process died first.
IMAGE = f"reader-kernel:test-{os.getpid()}.{uuid.uuid4().hex[:8]}"


def _lab_nb(asserts: list[str], source: str | None = None, n: int = 1) -> dict[str, Any]:
    nb = json.loads(F01)
    lab = next(c for c in nb["cells"] if c["id"] == "f01-lab")
    lab["metadata"]["reader"]["asserts"] = asserts
    lab["metadata"]["reader"].pop("verified", None)
    if source is not None:
        lab["source"] = source
    for i in range(1, n):
        extra = copy.deepcopy(lab)
        extra["id"] = f"f01-lab-{i}"
        nb["cells"].append(extra)
    return nb


async def test_labs_over_the_time_bound_are_not_run() -> None:
    verify = need("reader.pipeline.verify")
    b = spec_bounds()
    per_lab = b["lab_exec_timeout_s"] * b["lab_runs"] * (1 + 4)  # F01's lab has four asserts
    n = b["verify_job_timeout_s"] // per_lab + 1
    client = verify.VerifyClient(http=_never, ws=_never)
    report = await client.run(_lab_nb(json.loads(F01)["cells"][6]["metadata"]["reader"]["asserts"], n=n))
    assert [p.kind for p in report.problems] == ["labs_unverifiable"]
    assert report.problems[0].blocking and report.results == {}


async def test_no_labs_is_a_pass() -> None:
    verify = need("reader.pipeline.verify")
    nb = json.loads(F01)
    nb["cells"] = [c for c in nb["cells"] if c["metadata"]["reader"]["kind"] != "lab"]
    report = await verify.VerifyClient(http=_never, ws=_never).run(nb)
    assert report.results == {} and report.problems == []


async def _never(*_: Any, **__: Any) -> Any:
    raise AssertionError("no kernel call expected")


async def test_unreachable_kernel_is_verify_unavailable() -> None:
    verify = need("reader.pipeline.verify")
    upstream = need("reader.kernels.upstream")
    errors = need("reader.pipeline.errors")

    async def down(*_: Any, **__: Any) -> Any:
        raise upstream.UpstreamUnresolvable("gaierror")

    with pytest.raises(errors.VerifyUnavailable):
        await verify.VerifyClient(http=down, ws=down).run(_lab_nb(["True"]))


class _Reply:
    def __init__(self, status: int, data: Any) -> None:
        self.status, self._data = status, data

    def json(self) -> Any:
        return self._data


class FakeKernel:
    """Kernel Gateway and the control endpoint in memory: each run's source execution prints `outputs[run]`."""

    def __init__(self, outputs: list[str], chunk: int = 1) -> None:
        self.outputs, self.chunk = outputs, chunk
        self.run = -1
        self.nonce = 0

    async def http(self, method: str, host: str, port: int, path: str, body: Any = None) -> Any:
        if path == "/control/state":
            return _Reply(200, {"nonce": f"n{self.nonce}", "gateway": "up"})
        if path == "/control/exit":
            self.nonce += 1
            self.run += 1
            return _Reply(202, {})
        if path == "/api/kernels":
            return _Reply(201, {"id": "k"})
        return _Reply(204, {})

    async def ws(self, endpoint: Any, kernel: str) -> Any:
        return _Ws(self)


class _Ws:
    def __init__(self, kernel: FakeKernel) -> None:
        self.kernel, self.queue = kernel, []  # type: ignore[var-annotated]

    async def send(self, frame: str) -> None:
        request = json.loads(frame)
        parent = {"msg_id": request["header"]["msg_id"]}
        code = request["content"]["code"]
        if not code.startswith("assert"):
            text = self.kernel.outputs[self.kernel.run]
            pieces = [
                text[i : i + len(text) // self.kernel.chunk + 1]
                for i in range(0, len(text), len(text) // self.kernel.chunk + 1)
            ]
            for piece in pieces:
                self.queue.append(
                    {"parent_header": parent, "header": {"msg_type": "stream"}, "content": {"text": piece}}
                )
        self.queue.append(
            {"parent_header": parent, "header": {"msg_type": "execute_reply"}, "content": {"status": "ok"}}
        )
        self.queue.append(
            {
                "parent_header": parent,
                "header": {"msg_type": "status"},
                "content": {"execution_state": "idle"},
            }
        )

    async def recv(self) -> str:
        return json.dumps(self.queue.pop(0))

    async def close(self) -> None:
        return None


async def test_outputs_are_normalised_then_compared() -> None:
    """CRLF → LF and trailing whitespace do not count; any other difference fails the lab."""
    verify = need("reader.pipeline.verify")
    nb = _lab_nb(["True"])
    same = FakeKernel(["42\r\n", "42   \n\n"])
    report = await verify.VerifyClient(http=same.http, ws=same.ws).run(nb)
    assert report.results["f01-lab"]["outputs_identical"] is True
    assert report.results["f01-lab"]["passed"] is True
    differ = FakeKernel(["42\n", "43\n"])
    report = await verify.VerifyClient(http=differ.http, ws=differ.ws).run(nb)
    assert report.results["f01-lab"] == {
        "runs": 2,
        "passed": False,
        "outputs_identical": False,
        "input_sha256": report.results["f01-lab"]["input_sha256"],
    }
    assert [p.kind for p in report.problems] == ["lab_failed"]


async def test_lab_output_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Code review S4: past EXEC_OUTPUT_MAX of text in a run the lab fails and nothing more is kept."""
    verify = need("reader.pipeline.verify")
    assert verify.OUTPUT_MAX == need("reader.kernels.protocol").EXEC_OUTPUT_MAX
    monkeypatch.setattr(verify, "OUTPUT_MAX", 1000)
    big = "y" * 5000
    kernel = FakeKernel([big, big], chunk=10)
    report = await verify.VerifyClient(http=kernel.http, ws=kernel.ws).run(_lab_nb(["True"]))
    assert report.results["f01-lab"]["passed"] is False
    small = FakeKernel(["ok\n", "ok\n"])
    assert (await verify.VerifyClient(http=small.http, ws=small.ws).run(_lab_nb(["True"]))).problems == []


async def test_time_budget_counts_interrupt_grace_and_resets() -> None:
    """sum(steps) * (timeout + 5 s grace) * runs + runs * 30 s reset: `fits` one-step labs run, one more not."""
    verify = need("reader.pipeline.verify")
    b = spec_bounds()
    one_step = (b["lab_exec_timeout_s"] + 5) * b["lab_runs"]
    fixed = b["lab_runs"] * 30
    fits = int((b["verify_job_timeout_s"] - fixed) // one_step)
    assert fits * b["lab_exec_timeout_s"] * b["lab_runs"] < b["verify_job_timeout_s"] - one_step  # tighter
    for n, unverifiable in ((fits, False), (fits + 1, True)):
        kernel = FakeKernel(["1\n", "1\n"])
        report = await verify.VerifyClient(http=kernel.http, ws=kernel.ws).run(_lab_nb([], n=n))
        assert ([p.kind for p in report.problems] == ["labs_unverifiable"]) is unverifiable, n


# --- [image] -------------------------------------------------------------------------------------------------------


def _docker(*args: str, timeout: float = 1800) -> Any:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False, timeout=timeout)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def kernel() -> Iterator[tuple[int, int]]:
    if not DOCKER:
        pytest.skip("docker daemon not reachable")
    from ruamel.yaml import YAML

    sandbox = REPO / "sandbox"
    build = _docker(
        "build",
        "-q",
        "--platform",
        "linux/arm64",
        "-t",
        IMAGE,
        "-f",
        str(sandbox / "Dockerfile"),
        str(sandbox),
    )
    assert build.returncode == 0, build.stderr
    src = YAML(typ="safe").load((REPO / "deploy" / "compose.yaml").read_text())["x-kernel"]
    flags = ["--user", str(src["user"]), "--read-only"]
    for entry in src["tmpfs"]:
        flags += ["--tmpfs", str(entry)]
    flags += [
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--memory",
        str(src["mem_limit"]),
        "--memory-swap",
        str(src["memswap_limit"]),
        "--pids-limit",
        str(src["pids_limit"]),
        "--restart",
        "always",
    ]
    name = f"rk-pipeline-{uuid.uuid4().hex[:8]}"
    gw, ctl = _free_port(), _free_port()
    run = _docker(
        "run",
        "-d",
        "--name",
        name,
        "--label",
        "org.reader-ai.test=srv-pipeline",
        "-p",
        f"127.0.0.1:{gw}:8888",
        "-p",
        f"127.0.0.1:{ctl}:8889",
        *flags,
        IMAGE,
    )
    try:
        assert run.returncode == 0, run.stderr
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if _docker("exec", name, "/usr/local/bin/reader-kernel-health").returncode == 0:
                break
            time.sleep(0.5)
        yield gw, ctl
    finally:
        _docker("rm", "-f", name)
        _docker("image", "rm", IMAGE)  # untags; layers another tag uses stay


def _client(ports: tuple[int, int], timeout_s: float | None = None) -> Any:
    verify = need("reader.pipeline.verify")
    upstream = need("reader.kernels.upstream")
    kw = {} if timeout_s is None else {"exec_timeout_s": timeout_s}
    return verify.VerifyClient(endpoint=upstream.Endpoint("127.0.0.1", ports[0], ports[1]), **kw)


async def test_f01_lab_passes_twice_with_identical_outputs(kernel: tuple[int, int]) -> None:
    findings = need("reader.pipeline.findings")
    nb = json.loads(F01)
    report = await _client(kernel).run(nb)
    lab = next(cell for _, cell in findings.labs_of(nb))
    assert report.results == {
        "f01-lab": {
            "runs": 2,
            "passed": True,
            "outputs_identical": True,
            "input_sha256": findings.source_sha256(lab),
        }
    }
    assert report.problems == []
    assert report.labs_sha256 == findings.labs_sha256(nb)


async def test_tampered_assert_fails_the_lab(kernel: tuple[int, int]) -> None:
    nb = _lab_nb(["round(attacker_success(0.1, 5), 7) == 0.5"])
    report = await _client(kernel).run(nb)
    assert report.results["f01-lab"]["passed"] is False
    assert [(p.kind, p.blocking, p.stage) for p in report.problems] == [
        ("lab_failed", True, "lab_verification")
    ]


async def test_cell_past_the_timeout_fails_the_lab(kernel: tuple[int, int]) -> None:
    nb = _lab_nb(["True"], source="import time\ntime.sleep(30)\n")
    report = await _client(kernel, timeout_s=3).run(nb)
    assert report.results["f01-lab"]["passed"] is False
    assert report.problems and report.problems[0].kind == "lab_failed"
