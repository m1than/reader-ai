# @trace FR-SANDBOX-9
"""OPS-SANDBOX test isolation (spec header note): a test session's sweeps remove its own labelled objects and
those of dead sessions, never the objects of another live session on the shared Docker daemon."""

import ipaddress
import os
import subprocess
import sys
import uuid

from . import conftest
from .conftest import IMAGE, Build, create_network, docker, requires_docker_daemon

RUN_KEY = "org.reader-ai.test.sandbox-run"


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _pair(value: str, made: list[tuple[str, str]]) -> tuple[str, str]:
    """A labelled network and container. Each is registered in `made` as soon as it exists, so the caller's
    `finally` removes it even when a later create fails (OPS-SANDBOX header, test networks)."""
    tag = uuid.uuid4().hex[:8]
    net, name = f"rk-iso-net-{tag}", f"rk-iso-{tag}"
    create_network(net, f"{RUN_KEY}={value}")
    made.append((name, net))
    ran = docker("run", "-d", "--name", name, "--label", f"{RUN_KEY}={value}", "--network", net,
                 "--entrypoint", "sleep", IMAGE, "300")
    assert ran.returncode == 0, ran.stderr
    return name, net


def _remove(made: list[tuple[str, str]]) -> None:
    for name, net in made:
        docker("rm", "-f", name)
        docker("network", "rm", net)


def _exists(name: str, net: str) -> tuple[bool, bool]:
    return (docker("inspect", name).returncode == 0, docker("network", "inspect", net).returncode == 0)


TEST_POOL = ipaddress.ip_network("10.231.0.0/16")


def _subnets(net: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    out = docker("network", "inspect", "--format", "{{range .IPAM.Config}}{{.Subnet}} {{end}}", net)
    assert out.returncode == 0, out.stderr
    return [ipaddress.ip_network(s) for s in out.stdout.split()]


@requires_docker_daemon
def test_test_networks_use_explicit_subnets(image: Build) -> None:
    """Test networks take explicit subnets in 10.231.0.0/16, outside Docker's default address pools, which the
    live stack can exhaust (OPS-SANDBOX header, test networks)."""
    assert image.ok, image.log
    box = conftest.Sandbox(image)
    made: list[tuple[str, str]] = []
    try:
        _pair(conftest.LABEL.split("=", 1)[1], made)
        for net in (box._net(), made[0][1]):
            subnets = _subnets(net)
            assert subnets, net
            inside = [isinstance(s, ipaddress.IPv4Network) and s.subnet_of(TEST_POOL) for s in subnets]
            assert all(inside), (net, subnets)
    finally:
        box.cleanup()
        _remove(made)


@requires_docker_daemon
def test_session_sweep_spares_other_runs(image: Build) -> None:
    assert image.ok, image.log
    assert hasattr(conftest, "sweep"), "conftest has no run-scoped sweep"
    assert getattr(conftest, "RUN_KEY", None) == RUN_KEY
    own_value = conftest.LABEL.split("=", 1)[1]
    assert own_value.split(".", 1)[0].isdigit()
    made: list[tuple[str, str]] = []
    try:
        foreign = _pair(f"{os.getppid()}.{uuid.uuid4().hex[:8]}", made)   # a live process, not this session
        dead = _pair(f"{_dead_pid()}.{uuid.uuid4().hex[:8]}", made)
        own = _pair(own_value, made)
        conftest.sweep(conftest._stale)                                   # the start-of-session sweep
        conftest.sweep(lambda value: value == own_value)                  # the end-of-session sweep
        assert _exists(*foreign) == (True, True)
        assert _exists(*dead) == (False, False)
        assert _exists(*own) == (False, False)
    finally:
        _remove(made)


def _image_id(ref: str) -> str | None:
    proc = docker("image", "inspect", "--format", "{{.Id}}", ref)
    return proc.stdout.strip() if proc.returncode == 0 else None


@requires_docker_daemon
def test_session_sweep_spares_other_runs_tags(image: Build) -> None:
    """The image tag is per run: a concurrent session's tag survives this session's sweeps, a dead session's
    tag is removed, and `reader-kernel:local` is never touched."""
    assert image.ok, image.log
    run_id = conftest.LABEL.split("=", 1)[1]
    assert run_id == f"{os.getpid()}.{run_id.split('.', 1)[1]}"
    expected = f"reader-kernel:test-{run_id}"
    assert expected == IMAGE
    local_before = _image_id("reader-kernel:local")
    foreign = f"reader-kernel:test-{os.getppid()}.{uuid.uuid4().hex[:8]}"   # a live session, not this one
    dead = f"reader-kernel:test-{_dead_pid()}.{uuid.uuid4().hex[:8]}"
    ended = f"reader-kernel:test-{os.getpid()}.{uuid.uuid4().hex[:8]}"     # stands in for a run's end sweep
    for ref in (foreign, dead, ended):
        tagged = docker("tag", IMAGE, ref)
        assert tagged.returncode == 0, tagged.stderr
    try:
        conftest.sweep(conftest._stale)                                         # the start-of-session sweep
        conftest.sweep(lambda value: value == ended.split("test-", 1)[1])       # an end-of-session sweep
        assert _image_id(foreign) is not None
        assert _image_id(dead) is None
        assert _image_id(ended) is None
        assert _image_id(IMAGE) is not None          # this run's own tag is still in use
        assert _image_id("reader-kernel:local") == local_before
    finally:
        for ref in (foreign, dead, ended):
            docker("image", "rm", ref)
