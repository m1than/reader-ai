# @trace FR-SANDBOX-9
"""OPS-SANDBOX test isolation (spec header note): a test session's sweeps remove its own labelled objects and
those of dead sessions, never the objects of another live session on the shared Docker daemon."""

import os
import subprocess
import sys
import uuid

from . import conftest
from .conftest import IMAGE, Build, docker, requires_docker_daemon

RUN_KEY = "org.reader-ai.test.sandbox-run"


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _pair(value: str) -> tuple[str, str]:
    tag = uuid.uuid4().hex[:8]
    net = f"rk-iso-net-{tag}"
    made = docker("network", "create", "--internal", "--label", f"{RUN_KEY}={value}", net)
    assert made.returncode == 0, made.stderr
    name = f"rk-iso-{tag}"
    ran = docker("run", "-d", "--name", name, "--label", f"{RUN_KEY}={value}", "--network", net,
                 "--entrypoint", "sleep", IMAGE, "300")
    assert ran.returncode == 0, ran.stderr
    return name, net


def _exists(name: str, net: str) -> tuple[bool, bool]:
    return (docker("inspect", name).returncode == 0, docker("network", "inspect", net).returncode == 0)


@requires_docker_daemon
def test_session_sweep_spares_other_runs(image: Build) -> None:
    assert image.ok, image.log
    assert hasattr(conftest, "sweep"), "conftest has no run-scoped sweep"
    assert getattr(conftest, "RUN_KEY", None) == RUN_KEY
    own_value = conftest.LABEL.split("=", 1)[1]
    assert own_value.split(".", 1)[0].isdigit()
    foreign = _pair(f"{os.getppid()}.{uuid.uuid4().hex[:8]}")   # a live process that is not this session
    dead = _pair(f"{_dead_pid()}.{uuid.uuid4().hex[:8]}")
    own = _pair(own_value)
    try:
        conftest.sweep(conftest._stale)                                   # the start-of-session sweep
        conftest.sweep(lambda value: value == own_value)                  # the end-of-session sweep
        assert _exists(*foreign) == (True, True)
        assert _exists(*dead) == (False, False)
        assert _exists(*own) == (False, False)
    finally:
        for name, net in (foreign, dead, own):
            docker("rm", "-f", name)
            docker("network", "rm", net)
