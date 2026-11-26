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
    SERVER_SERVICES,
    all_renders,
    render,
    requires_docker_cli,
)

pytestmark = requires_docker_cli

DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
TAGGED_DIGEST = re.compile(r"^[^@:]+:[^@:/]+@sha256:[0-9a-f]{64}$")
LOCAL_IMAGES = {
    "reader-server:local", "reader-postgres:local", "reader-kernel:local", "reader-agent-proxy:local",
    "reader-caddy:local", "reader-embed:local",
    "reader-mlflow:local",  # FR-DEPLOY-13 (paired SRV-OBS change)
}
BASE = {"caddy", "api", "worker", "worker-judge", "postgres", "embed"}  # embed: SRV-SEARCH; worker-judge: FR-DEPLOY-16


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


def test_agent_proxy_never_pulls() -> None:
    services = render(("agents",))["services"]
    assert services["agent-proxy"]["pull_policy"] == "never"
    assert all("pull_policy" not in body for name, body in services.items() if name != "agent-proxy")


def test_server_roles_share_one_built_image() -> None:
    services = render(("mcp",))["services"]
    for role in ("api", "worker", "mcp"):
        svc = services[role]
        assert svc["image"] == "reader-server:local"
        assert svc["build"]["dockerfile"] == "deploy/images/server.Dockerfile"
        assert svc["command"][:3] == ["reader-server", "--role", role]
    # the worker's Agent SDK (SRV-AGENT `agent` extra); agent-proxy is built from its own Dockerfile
    assert "/opt/reader[agent]" in (DEPLOY / "images" / "server.Dockerfile").read_text()
    proxy = render(("agents",))["services"]["agent-proxy"]
    assert proxy["build"]["dockerfile"] == "deploy/images/agent-proxy.Dockerfile"


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
    "agent-proxy.Dockerfile": {"server/pyproject.toml", "server/src"},  # SRV-AGENT FR-AGENT-1
    "postgres.Dockerfile": {"reader-postgres-entrypoint.sh"},
    "caddy.Dockerfile": {
        "web/package.json", "web/package-lock.json", "web/vendor", "web/index.html", "web/vite.config.ts",
        "web/tsconfig.json", "web/src", "/web/dist",
    },
    # Paired SRV-SEARCH change (FR-DEPLOY-2, ADR-0028): only the embed module files and the hash lock.
    "embed.Dockerfile": {
        "deploy/images/embed.requirements.lock", "server/src/reader/search/__init__.py",
        "server/src/reader/search/embed_service.py", "server/src/reader/search/fetch_model.py",
        "server/src/reader/search/models.lock.json",
    },
    # FR-DEPLOY-13 (paired SRV-OBS change): the pinned MLflow image plus a data directory and, since SRV-OBS
    # FR-OBS-12 (c), the generated all-zero model catalog that replaces MLflow's bundled one.
    "mlflow.Dockerfile": {"deploy/otel/mlflow-catalog/anthropic.json"},
}


def test_dockerfile_build_inputs_are_an_allow_list() -> None:
    dockerfiles = _dockerfiles()
    assert {p.name for p in dockerfiles} == set(COPY_ALLOW_LIST)
    for path in dockerfiles:
        assert not _instructions(path, "ADD"), path
        sources = {src for args in _instructions(path, "COPY") for src in args[:-1]}
        assert sources == COPY_ALLOW_LIST[path.name], (path.name, sources)
        # COPY --from=<x> must name a stage of this Dockerfile, never an external image.
        lines = [ln.split() for ln in path.read_text().splitlines() if ln.split()]
        stages = {ln[3] for ln in lines if ln[0].upper() == "FROM" and len(ln) >= 4 and ln[2].upper() == "AS"}
        copies = [ln[1:] for ln in lines if ln[0].upper() == "COPY"]
        froms = [arg.split("=", 1)[1] for args in copies for arg in args if arg.startswith("--from=")]
        assert all(f in stages for f in froms), (path.name, froms, stages)


def test_caddy_build_runs_no_install_scripts() -> None:
    text = (DEPLOY / "images" / "caddy.Dockerfile").read_text()
    installs = [ln for ln in text.splitlines() if ln.strip().startswith("RUN") and "npm ci" in ln]
    assert installs, "caddy.Dockerfile runs no npm ci"
    assert all("--ignore-scripts" in ln for ln in installs), installs


def test_server_dockerignore_excludes_caches_and_env_files() -> None:
    path = DEPLOY / "images" / "server.Dockerfile.dockerignore"
    assert path.exists(), path
    patterns = {ln.strip() for ln in path.read_text().splitlines() if ln.strip() and not ln.startswith("#")}
    assert {"**/__pycache__", "**/.env*"} <= patterns, patterns


def test_embed_dockerignore_excludes_caches_and_env_files() -> None:
    path = DEPLOY / "images" / "embed.Dockerfile.dockerignore"
    assert path.exists(), path
    patterns = {ln.strip() for ln in path.read_text().splitlines() if ln.strip() and not ln.startswith("#")}
    assert {"**/__pycache__", "**/.env*"} <= patterns, patterns


def test_embed_dockerfile_installs_only_hashed_binaries_and_pins_the_model() -> None:
    # Paired SRV-SEARCH change (FR-DEPLOY-2): hash-locked wheels only; the model fetched, verified at build.
    text = (DEPLOY / "images" / "embed.Dockerfile").read_text()
    installs = [ln for ln in text.splitlines() if "pip install" in ln]
    assert installs, "embed.Dockerfile installs nothing"
    for line in installs:
        assert "--require-hashes" in line, line
        assert "--only-binary" in line, line
        assert "embed.requirements.lock" in line, line
    assert "fetch_model" in text
    assert "HF_HUB_OFFLINE=1" in text
    assert "TRANSFORMERS_OFFLINE=1" in text
    users = [ln.split()[1] for ln in text.splitlines() if ln.split()[:1] == ["USER"]]
    assert users == ["10003:10003"], users
    lock = (DEPLOY / "images" / "embed.requirements.lock").read_text()
    for pin in ("torch==2.15.0", "transformers==5.23.0", "sentence-transformers==6.3.0"):
        assert pin in lock, pin
    reqs = [ln for ln in lock.splitlines() if ln and not ln.startswith((" ", "#", "-"))]
    assert reqs
    assert all("==" in ln for ln in reqs), reqs


def test_postgres_image_installs_pinned_pgvector() -> None:
    # Paired SRV-SEARCH change (FR-DEPLOY-2): pgvector from the base image's PGDG source, exact version.
    text = (DEPLOY / "images" / "postgres.Dockerfile").read_text()
    assert "postgresql-18-pgvector=0.8.7-1.pgdg13+2" in text


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
    # server services and agent-proxy: the override's reader.yaml bind (worker-judge: FR-DEPLOY-16 f)
    assert {n for n, b in binds.items() if b} == {"caddy", *SERVER_SERVICES, "agent-proxy"}
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


SETUP_BIND_TARGET = "/etc/reader/mlflow-setup.yaml"  # FR-DEPLOY-15 (d), (g): the worker's one read-only bind


def test_base_has_no_bind_on_server_roles() -> None:
    for profiles in PROFILE_SETS:
        services = render(profiles)["services"]
        for role in SERVER_SERVICES:
            if role not in services:
                continue
            binds = _binds(services[role])
            if role == "worker":  # FR-DEPLOY-15 (g): exactly the setup file, read-only
                got = [(b["target"], b.get("read_only")) for b in binds]
                assert got == [(SETUP_BIND_TARGET, True)], profiles
            else:
                assert not binds, (role, profiles)


def test_override_binds_the_config_file_into_server_roles_and_agent_proxy_only() -> None:
    services = render(PROFILES, override=True)["services"]
    with_config = {
        name for name, svc in services.items()
        if any(m["target"] == CONFIG_BIND["target"] for m in svc.get("volumes") or [])
    }
    assert with_config == {*SERVER_SERVICES, "agent-proxy"}
    for role in (*SERVER_SERVICES, "agent-proxy"):
        binds = [b for b in _binds(services[role]) if b["target"] != SETUP_BIND_TARGET or role != "worker"]
        assert binds == [CONFIG_BIND], role  # FR-DEPLOY-15 (g): the worker also keeps its setup-file bind
    for role in SERVER_SERVICES:
        assert services[role]["environment"] == {"READER_CONFIG": CONFIG_BIND["target"]}, role
    assert not services["agent-proxy"].get("environment")


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
            keys = FORBIDDEN_KEYS
            if name == "mlflow-migrate":  # FR-DEPLOY-15 (g): the one network-less one-shot
                assert svc.get("network_mode") == "none", name
                keys = tuple(k for k in FORBIDDEN_KEYS if k != "network_mode")
            assert not any(svc.get(k) for k in keys), name
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
