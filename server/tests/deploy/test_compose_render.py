# @trace FR-DEPLOY-1, FR-DEPLOY-2, FR-DEPLOY-5, FR-DEPLOY-6, INV-DEPLOY-1, INV-DEPLOY-3
# @trace INV-DEPLOY-6, INV-DEPLOY-7, INV-DEPLOY-9, INV-DEPLOY-10, VEC-DEPLOY-3
"""OPS-DEPLOY [config]: services by profile, images and build inputs, volumes and mounts, ports, forbidden
options."""

import re
from pathlib import Path
from typing import Any

import pytest

from .conftest import (
    COMPOSE,
    DEPLOY,
    PROFILE_SETS,
    PROFILES,
    SERVER_ROLES,
    all_renders,
    render,
    requires_docker_cli,
)

pytestmark = requires_docker_cli

DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
TAGGED_DIGEST = re.compile(r"^[^@:]+:[^@:/]+@sha256:[0-9a-f]{64}$")
LOCAL_IMAGES = {
    "reader-server:local", "reader-postgres:local", "reader-kernel:local", "reader-agent-proxy:local",
    "reader-caddy:local",
}
BASE = {"caddy", "api", "worker", "postgres"}


@pytest.mark.parametrize(
    ("profiles", "expected"),
    [
        ((), BASE),
        (("kernels",), BASE | {"kernel-verify", "kernel-agent", "kernel-pair-1"}),
        (("agents",), BASE | {"agent-proxy"}),
        (("mcp",), BASE | {"mcp"}),
    ],
)
def test_services_by_profile(profiles: tuple[str, ...], expected: set[str]) -> None:
    assert set(render(profiles)["services"]) == expected


def test_server_roles_share_one_built_image() -> None:
    services = render(("mcp",))["services"]
    for role in ("api", "worker", "mcp"):
        svc = services[role]
        assert svc["image"] == "reader-server:local"
        assert svc["build"]["dockerfile"] == "deploy/images/server.Dockerfile"
        assert svc["command"][:3] == ["reader-server", "--role", role]


def _third_party_images(model: dict[str, Any]) -> set[str]:
    return {svc["image"] for svc in model["services"].values() if svc["image"] not in LOCAL_IMAGES}


def test_images_pinned_by_digest_in_compose() -> None:
    # Since V.OPS-DEPLOY-web every image in compose.yaml is built locally (caddy too); third-party
    # bases are pinned in the Dockerfiles (test_dockerfile_from_lines_pinned). A later one is checked here.
    images = set().union(*(_third_party_images(m) for m in all_renders()))
    assert all(TAGGED_DIGEST.match(img) for img in images), images
    assert not any(":latest" in img for img in images)


def _dockerfiles() -> list[Path]:
    """Every file under deploy/** named `Dockerfile` or `*.Dockerfile` (INV-DEPLOY-7)."""
    return sorted(
        p for p in DEPLOY.rglob("*") if p.is_file() and (p.name == "Dockerfile" or p.suffix == ".Dockerfile")
    )


def _instructions(path: Path, keyword: str) -> list[list[str]]:
    """Arguments of each `keyword` instruction, flags such as `--platform=` or `--chmod=` removed."""
    lines = path.read_text().splitlines()
    return [
        [arg for arg in line.split()[1:] if not arg.startswith("--")]
        for line in lines if line.split()[:1] and line.split()[0].upper() == keyword
    ]


def test_dockerfile_from_lines_pinned() -> None:
    dockerfiles = _dockerfiles()
    assert {p.name for p in dockerfiles} >= {"server.Dockerfile", "postgres.Dockerfile", "caddy.Dockerfile"}
    for path in dockerfiles:
        froms = [args[0] for args in _instructions(path, "FROM")]
        assert froms, path
        assert all(TAGGED_DIGEST.match(ref) for ref in froms), (path, froms)
        assert not any(":latest@" in ref for ref in froms), (path, froms)


COPY_ALLOW_LIST = {
    "server.Dockerfile": {"server/pyproject.toml", "server/src", "deploy/images/wait-for-secret.sh"},
    "postgres.Dockerfile": {"reader-postgres-entrypoint.sh"},
    "caddy.Dockerfile": {
        "web/package.json", "web/package-lock.json", "web/vendor", "web/index.html", "web/vite.config.ts",
        "web/tsconfig.json", "web/src", "/web/dist",
    },
}


def test_dockerfile_build_inputs_are_an_allow_list() -> None:
    dockerfiles = _dockerfiles()
    assert {p.name for p in dockerfiles} == set(COPY_ALLOW_LIST)
    for path in dockerfiles:
        assert not _instructions(path, "ADD"), path
        sources = {src for args in _instructions(path, "COPY") for src in args[:-1]}
        assert sources == COPY_ALLOW_LIST[path.name], (path.name, sources)


def test_server_dockerignore_excludes_caches_and_env_files() -> None:
    path = DEPLOY / "images" / "server.Dockerfile.dockerignore"
    assert path.exists(), path
    patterns = {ln.strip() for ln in path.read_text().splitlines() if ln.strip() and not ln.startswith("#")}
    assert {"**/__pycache__", "**/.env*"} <= patterns, patterns


def test_caddy_dockerignore_excludes_build_outputs_and_env_files() -> None:
    path = DEPLOY / "images" / "caddy.Dockerfile.dockerignore"
    assert path.exists(), path
    patterns = {ln.strip() for ln in path.read_text().splitlines() if ln.strip() and not ln.startswith("#")}
    assert {"**/node_modules", "**/dist", "**/storybook-static", "**/.env*"} <= patterns, patterns


def _mounts(model: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {name: svc.get("volumes") or [] for name, svc in model["services"].items()}


def test_volumes_and_mounts() -> None:
    model = render(("kernels", "agents", "mcp"), override=True)
    mounts = _mounts(model)
    named = {
        name: {m["source"] for m in ms if m["type"] == "volume"} for name, ms in mounts.items()
    }
    binds = {name: [m for m in ms if m["type"] == "bind"] for name, ms in mounts.items()}
    assert named["postgres"] == {"pgdata"}
    assert {n for n, vols in named.items() if "pgdata" in vols} == {"postgres"}
    assert {n for n, vols in named.items() if "blobs" in vols} == {"api", "worker"}
    assert {n for n, b in binds.items() if b} == {"caddy", *SERVER_ROLES}  # server roles: the override's bind
    caddy_binds = {(m["target"], m.get("read_only", False)) for m in binds["caddy"]}
    assert caddy_binds == {("/etc/caddy/Caddyfile", True)}  # the SPA is in the reader-caddy image
    labels = {
        name: (v.get("labels") or {}).get("org.reader-ai.backup") for name, v in model["volumes"].items()
    }
    assert labels == {
        "pgdata": "include", "blobs": "include", "caddy_data": "exclude", "caddy_config": "include",
    }


CONFIG_BIND = {
    "type": "bind",
    "source": "/tmp/reader-ai-test/reader.yaml",  # noqa: S108 - the fixture's path, equal to OPS-CLI VEC-CLI-2
    "target": "/etc/reader/reader.yaml",
    "read_only": True,
    "bind": {"create_host_path": False},
}


def _binds(svc: dict[str, Any]) -> list[dict[str, Any]]:
    return [m for m in svc.get("volumes") or [] if m["type"] == "bind"]


def test_base_has_no_bind_on_server_roles() -> None:
    for profiles in PROFILE_SETS:
        services = render(profiles)["services"]
        assert not any(_binds(services[r]) for r in SERVER_ROLES if r in services), profiles


def test_override_binds_the_config_file_into_server_roles_only() -> None:
    services = render(PROFILES, override=True)["services"]
    with_config = {
        name for name, svc in services.items()
        if any(m["target"] == CONFIG_BIND["target"] for m in svc.get("volumes") or [])
    }
    assert with_config == set(SERVER_ROLES)
    for role in SERVER_ROLES:
        assert _binds(services[role]) == [CONFIG_BIND], role
        assert services[role]["environment"] == {"READER_CONFIG": CONFIG_BIND["target"]}, role


def test_caddy_volumes_only_in_caddy() -> None:
    for model in all_renders():
        users = {
            name for name, ms in _mounts(model).items()
            for m in ms if m["type"] == "volume" and m["source"] in {"caddy_data", "caddy_config"}
        }
        assert users == {"caddy"}


def test_only_caddy_publishes_on_loopback() -> None:
    for model in all_renders():
        published = {name: svc.get("ports") or [] for name, svc in model["services"].items()}
        assert {n for n, ps in published.items() if ps} == {"caddy"}
        assert [(p["host_ip"], p["target"], str(p["published"])) for p in published["caddy"]] == [
            ("127.0.0.1", 443, "443")
        ]
        assert all(p.get("host_ip") == "127.0.0.1" for ps in published.values() for p in ps)


DOCKER_SOCKET = re.compile(r"^/(var/)?run/docker")


def test_no_docker_socket() -> None:
    for model in all_renders():
        for name, ms in _mounts(model).items():
            for m in ms:
                assert not DOCKER_SOCKET.match(str(m.get("source", ""))), (name, m)
                assert not DOCKER_SOCKET.match(str(m.get("target", ""))), (name, m)


FORBIDDEN_KEYS = ("network_mode", "cap_add", "volumes_from", "extra_hosts", "devices")
HOST_NAMESPACES = ("pid", "ipc", "uts", "cgroup", "userns_mode")
UNCONFINED = re.compile(r"(seccomp|apparmor)[=:]unconfined")  # current and legacy forms (INV-DEPLOY-10)


def test_unconfined_pattern_covers_both_forms() -> None:
    for opt in ("seccomp=unconfined", "seccomp:unconfined", "apparmor=unconfined", "apparmor:unconfined"):
        assert UNCONFINED.search(opt), opt
    assert not UNCONFINED.search("no-new-privileges:true")


def test_forbidden_options() -> None:
    for model in all_renders():
        for name, svc in model["services"].items():
            assert not any(svc.get(k) for k in FORBIDDEN_KEYS), name
            assert not svc.get("privileged"), name
            assert all(svc.get(ns) != "host" for ns in HOST_NAMESPACES), name
            opts = svc.get("security_opt") or []
            assert not any(UNCONFINED.search(opt) for opt in opts), (name, opts)


@pytest.mark.parametrize("profiles", PROFILE_SETS)
@pytest.mark.parametrize("override", [False, True])
def test_renders_without_sandbox_or_secrets(profiles: tuple[str, ...], override: bool) -> None:
    assert COMPOSE.exists()
    assert "sandbox/" not in COMPOSE.read_text(), "compose.yaml must not depend on sandbox/"
    model = render(profiles, override=override)  # render() runs with every READER_* variable removed
    assert model["services"]
