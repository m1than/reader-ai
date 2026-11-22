# @trace FR-KERNEL-2, FR-KERNEL-5, FR-KERNEL-10
"""SRV-KERNEL against the real kernel image (OPS-SANDBOX's reader-kernel:local), run with the x-kernel flags
and `--restart always`, its ports published on 127.0.0.1: first use resets the container (a new nonce), a cell
runs through the relay, and an interrupted long cell (sleep and a busy loop) replies within the 5 s grace."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from .conftest import REPO, FakeBrowser, execute, load, requires_docker, settle

pytestmark = [pytest.mark.anyio, requires_docker]
IMAGE = "reader-kernel:local"
LABEL = "org.reader-ai.test=srv-kernel"


def _docker(*args: str, timeout: float = 600) -> Any:
    import subprocess

    return subprocess.run(  # noqa: S603
        ["docker", *args], capture_output=True, text=True, check=False, timeout=timeout  # noqa: S607
    )


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _template_flags() -> list[str]:
    from ruamel.yaml import YAML

    src = YAML(typ="safe").load((REPO / "deploy" / "compose.yaml").read_text())["x-kernel"]
    flags = ["--user", str(src["user"]), "--read-only"]
    for entry in src["tmpfs"]:
        flags += ["--tmpfs", str(entry)]
    flags += [
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--memory", str(src["mem_limit"]), "--memory-swap", str(src["memswap_limit"]),
        "--pids-limit", str(src["pids_limit"]), "--restart", "always",
    ]
    return flags


@pytest.fixture(scope="module")
def kernel() -> Iterator[tuple[int, int]]:
    sandbox = REPO / "sandbox"
    dockerfile = str(sandbox / "Dockerfile")
    build = _docker(
        "build", "-q", "--platform", "linux/arm64", "-t", IMAGE, "-f", dockerfile, str(sandbox), timeout=1800
    )
    assert build.returncode == 0, build.stderr
    name = f"rk-srvkernel-{uuid.uuid4().hex[:8]}"
    # Fixed host ports: an ephemeral published port is chosen anew on every restart, and the reset restarts.
    gw, ctl = _free_port(), _free_port()
    run = _docker("run", "-d", "--name", name, "--label", LABEL, "-p", f"127.0.0.1:{gw}:8888",
                  "-p", f"127.0.0.1:{ctl}:8889", *_template_flags(), IMAGE)
    assert run.returncode == 0, run.stderr
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            probe = _docker("exec", name, "/usr/local/bin/reader-kernel-health")
            if probe.returncode == 0:
                break
            time.sleep(0.5)
        yield gw, ctl
    finally:
        _docker("rm", "-f", name)


def _service(ports: tuple[int, int]) -> Any:
    service = load("reader.kernels.service")
    upstream = load("reader.kernels.upstream")
    return service.KernelService(lambda _: upstream.Endpoint("127.0.0.1", ports[0], ports[1]))


async def _run_cell(svc: Any, session: Any, msg_id: str, code: str, wait_s: float) -> FakeBrowser:
    relay = load("reader.kernels.relay")
    hub = relay.Hub(svc, session)
    session.hub = hub
    browser = FakeBrowser()

    async def ok() -> bool:
        return True

    att = hub.attach(browser, ok)
    assert await hub.start()
    task = asyncio.ensure_future(hub.serve(att))
    browser.send(execute(msg_id, code=code))
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if any(m.get("header", {}).get("msg_type") == "execute_reply" for m in browser.received):
            break
        await asyncio.sleep(0.05)
    browser.disconnect()
    await settle()
    task.cancel()
    return browser


async def test_first_use_reset_and_a_cell(kernel: tuple[int, int]) -> None:
    svc = _service(kernel)
    upstream = load("reader.kernels.upstream")
    before = json.loads((await upstream.http("GET", "127.0.0.1", kernel[1], "/control/state")).body)["nonce"]
    status, session = await svc.create("ivan", "L02")
    assert status == 201
    assert svc.user("ivan").nonce not in (None, before)
    browser = await _run_cell(svc, session, "m1", "print(1)", 30)
    streams = [m["content"]["text"] for m in browser.received if m["header"].get("msg_type") == "stream"]
    assert streams == ["1\n"]


@pytest.mark.parametrize("code", ["import time\ntime.sleep(600)", "while True:\n    pass"])
async def test_interrupt_replies_within_grace(kernel: tuple[int, int], code: str,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    relay = load("reader.kernels.relay")
    monkeypatch.setattr(relay, "EXEC_TIMEOUT_S", 1.0)
    svc = _service(kernel)
    _, session = await svc.create("ivan", "L-sleep" if "sleep" in code else "L-busy")
    t0 = time.monotonic()
    browser = await _run_cell(svc, session, "long", code, 1.0 + relay.INTERRUPT_GRACE_S + 5)
    elapsed = time.monotonic() - t0
    print(f"interrupted {code.splitlines()[-1].strip()!r}: reply after {elapsed:.2f} s")
    assert ("timeout", "long") in browser.notices()
    reply = next(m for m in browser.received if m.get("header", {}).get("msg_type") == "execute_reply")
    assert reply["content"]["status"] == "error"
    assert elapsed < 1.0 + relay.INTERRUPT_GRACE_S
    assert ("kernel_reset", None) not in browser.notices()
