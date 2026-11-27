"""Helpers for the OPS-DEPLOY [config] and [image] tests.

[config] tests need only the docker CLI (`docker compose config` renders without a daemon); they skip when the
CLI is absent. [image] tests need a running daemon; they skip when `docker info` fails. [image: registry]
tests also query the remote registry and run only with READER_TEST_REGISTRY=1.
Inputs that do not exist yet make a test fail on an assertion inside its body, never at collection.
"""

import itertools
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

REPO = Path(__file__).resolve().parents[3]
DEPLOY = REPO / "deploy"
COMPOSE = DEPLOY / "compose.yaml"
CADDYFILE = DEPLOY / "Caddyfile"
EXAMPLE = DEPLOY / "reader.example.yaml"
FIXTURES = REPO / "spec" / "fixtures" / "OPS-DEPLOY"
OVERRIDE = FIXTURES / "cli-override.example.yaml"
REACHABILITY = FIXTURES / "reachability.yaml"

PROFILES = ("kernels", "agents", "mcp", "observability")  # observability: FR-DEPLOY-13 (paired SRV-OBS)
PROFILE_SETS: list[tuple[str, ...]] = [
    combo for n in range(len(PROFILES) + 1) for combo in itertools.combinations(PROFILES, n)
]
KERNEL_PREFIX = "kernel-"
# worker-judge: FR-DEPLOY-16
SECRET_CONSUMERS = {"postgres", "api", "worker", "worker-judge", "mcp", "agent-proxy"}
SERVER_ROLES = ("api", "worker", "mcp")
LANE = "worker-judge"  # FR-DEPLOY-16: the worker role on the judge lane
SERVER_SERVICES = (*SERVER_ROLES, LANE)  # every service running the server image

requires_docker_cli = pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")


def _daemon_up() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0  # noqa: S607


requires_docker_daemon = pytest.mark.skipif(not _daemon_up(), reason="docker daemon not reachable")

# [image: registry]: queries the remote registry on every run; CI and on request only
# (Docker Hub rate-limits anonymous requests; HTTP 429 seen on 2026-11-20).
registry = pytest.mark.registry
requires_registry_opt_in = pytest.mark.skipif(
    os.environ.get("READER_TEST_REGISTRY") != "1",
    reason="registry check; set READER_TEST_REGISTRY=1 to run",
)


def load_yaml(path: Path) -> Any:
    assert path.exists(), f"{path} does not exist"
    return YAML(typ="safe").load(path.read_text(encoding="utf-8"))


def clean_env() -> dict[str, str]:
    """The caller's environment without any secret variable (INV-DEPLOY-9)."""
    return {k: v for k, v in os.environ.items() if not k.startswith("READER_")}


def render(profiles: tuple[str, ...] = (), *, override: bool = False) -> dict[str, Any]:
    """`docker compose config --format json` of deploy/compose.yaml, optionally with the override fixture."""
    assert COMPOSE.exists(), "deploy/compose.yaml does not exist"
    files = ["-f", str(COMPOSE)] + (["-f", str(OVERRIDE)] if override else [])
    args = [arg for p in profiles for arg in ("--profile", p)]
    proc = subprocess.run(  # noqa: S603
        ["docker", "compose", *files, *args, "config", "--format", "json"],  # noqa: S607
        capture_output=True, text=True, cwd=DEPLOY, env=clean_env(), check=False,
    )
    assert proc.returncode == 0, proc.stderr
    model: dict[str, Any] = json.loads(proc.stdout)
    return model


def all_renders() -> list[dict[str, Any]]:
    """Every allowed profile combination, with and without the override (the sets of INV-DEPLOY-1)."""
    return [render(p, override=o) for p in PROFILE_SETS for o in (False, True)]


def full_render() -> dict[str, Any]:
    return render(PROFILES, override=True)


def kernel_services(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {name: svc for name, svc in model["services"].items() if name.startswith(KERNEL_PREFIX)}


def networks_of(svc: dict[str, Any]) -> set[str]:
    return set((svc.get("networks") or {}).keys())


def sharing_pairs(model: dict[str, Any]) -> set[frozenset[str]]:
    services = model["services"]
    return {
        frozenset((a, b))
        for a, b in itertools.combinations(sorted(services), 2)
        if networks_of(services[a]) & networks_of(services[b])
    }


def tmpfs_map(svc: dict[str, Any]) -> dict[str, set[str]]:
    """tmpfs entries as mount point → option set."""
    out: dict[str, set[str]] = {}
    for entry in svc.get("tmpfs") or []:
        target, _, opts = str(entry).partition(":")
        out[target] = set(filter(None, opts.split(",")))
    return out


def run_ok(*cmd: str) -> str:
    proc = subprocess.run(list(cmd), capture_output=True, text=True, check=False)  # noqa: S603
    assert proc.returncode == 0, proc.stderr
    return proc.stdout
