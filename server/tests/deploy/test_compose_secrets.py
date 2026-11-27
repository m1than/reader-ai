# @trace FR-DEPLOY-9, FR-DEPLOY-10, FR-DEPLOY-11, INV-DEPLOY-2, FR-DEPLOY-16
"""OPS-DEPLOY [config]: process hardening, memory limits, the ADR-0026 secret mounts, health checks."""

import re

from .conftest import (
    CADDYFILE,
    COMPOSE,
    DEPLOY,
    SECRET_CONSUMERS,
    SERVER_ROLES,
    SERVER_SERVICES,
    all_renders,
    full_render,
    requires_docker_cli,
    tmpfs_map,
)

pytestmark = requires_docker_cli

NNP = "no-new-privileges:true"
GIB = 1024**3
LIMITS = {
    "postgres": GIB, "api": GIB, "mcp": GIB, "worker": 2 * GIB, "agent-proxy": 256 * 1024**2,
    "worker-judge": 2 * GIB,  # FR-DEPLOY-16 (b): initial value, judge-gate measures
}


def _uid(user: str) -> str:
    return user.split(":")[0]


def test_server_roles_and_proxy_hardened() -> None:
    services = full_render()["services"]
    for name in ("api", "worker", "worker-judge", "mcp", "agent-proxy"):
        svc = services[name]
        assert svc.get("user"), name
        assert _uid(svc["user"]) not in {"0", "root"}, name
        assert svc.get("read_only") is True, name
        assert any(path.startswith("/home/") for path in tmpfs_map(svc)), name
        assert svc.get("cap_drop") == ["ALL"], name
        assert NNP in (svc.get("security_opt") or []), name


def test_postgres_hardened() -> None:
    pg = full_render()["services"]["postgres"]
    assert pg["image"] == "reader-postgres:local"
    assert pg["user"] == "999:999"
    assert pg.get("read_only") is True
    assert pg.get("cap_drop") == ["ALL"]
    assert NNP in pg.get("security_opt", [])
    assert {"/var/run/postgresql", "/tmp"} <= set(tmpfs_map(pg))  # noqa: S108 - container paths


def test_embed_hardened_and_not_a_consumer() -> None:
    # Paired SRV-SEARCH change (FR-DEPLOY-9, ADR-0028): non-root, read-only, no capabilities, no secrets, 3g.
    for model in all_renders():
        embed = model["services"]["embed"]
        assert embed["image"] == "reader-embed:local"
        assert embed["build"]["dockerfile"] == "deploy/images/embed.Dockerfile"
        assert embed["user"] == "10003:10003"
        assert embed.get("init") is True
        assert embed.get("read_only") is True
        assert embed.get("cap_drop") == ["ALL"]
        assert NNP in (embed.get("security_opt") or [])
        assert set(tmpfs_map(embed)) == {"/tmp"}  # noqa: S108 - container path
        assert int(embed["mem_limit"]) == 3 * GIB
        assert embed["memswap_limit"] == embed["mem_limit"]
        assert not embed.get("environment")
        assert not embed.get("volumes")
        assert "depends_on" not in embed
        for name, svc in model["services"].items():
            assert "embed" not in (svc.get("depends_on") or {}), name


def test_caddy_keeps_capabilities_without_additions() -> None:
    caddy = full_render()["services"]["caddy"]
    assert not caddy.get("cap_add")
    assert NNP in (caddy.get("security_opt") or [])


def test_consumer_memory_limits_equal_swap() -> None:
    services = full_render()["services"]
    for name, limit in LIMITS.items():
        assert int(services[name]["mem_limit"]) == limit, name
        assert services[name]["memswap_limit"] == services[name]["mem_limit"], name


def test_no_compose_secrets_or_env_files() -> None:
    for model in all_renders():
        assert not model.get("secrets")
        for name, svc in model["services"].items():
            assert not svc.get("secrets"), name
            assert not svc.get("env_file"), name


def test_secret_tmpfs_on_exactly_the_consumers() -> None:
    services = full_render()["services"]
    with_mount = {name for name, svc in services.items() if "/run/secrets" in tmpfs_map(svc)}
    assert with_mount == SECRET_CONSUMERS
    for name in SECRET_CONSUMERS:
        opts = tmpfs_map(services[name])["/run/secrets"]
        assert {"mode=0700", "size=256k", f"uid={_uid(services[name]['user'])}"} <= opts, (name, opts)


def test_health_dependencies_on_postgres() -> None:
    services = full_render()["services"]
    for name in SERVER_SERVICES:
        assert services[name]["depends_on"]["postgres"]["condition"] == "service_healthy", name


def test_entrypoint_wrappers_wait_for_their_file() -> None:
    files = (
        "server.Dockerfile", "postgres.Dockerfile", "reader-postgres-entrypoint.sh", "wait-for-secret.sh",
    )
    missing = [name for name in files if not (DEPLOY / "images" / name).exists()]
    assert not missing, missing
    server = (DEPLOY / "images" / "server.Dockerfile").read_text()
    postgres = (DEPLOY / "images" / "postgres.Dockerfile").read_text()
    assert "wait-for-secret" in server
    assert "reader-postgres-entrypoint" in postgres
    wrapper = (DEPLOY / "images" / "reader-postgres-entrypoint.sh").read_text()
    assert "/run/secrets/db-password" in wrapper
    assert "PGDATA" in wrapper  # waits and sets POSTGRES_PASSWORD_FILE only while PGDATA is empty
    waiter = (DEPLOY / "images" / "wait-for-secret.sh").read_text()
    assert "/run/secrets/" in waiter


def test_wrappers_exit_on_sigterm() -> None:
    for name in ("wait-for-secret.sh", "reader-postgres-entrypoint.sh"):
        path = DEPLOY / "images" / name
        assert path.exists(), name
        assert "trap 'exit 143' TERM" in path.read_text(), name


# start_period 60s, as Compose renders it
HEALTH_TIMING = {"interval": "2s", "timeout": "2s", "retries": 3, "start_period": "1m0s"}


def test_every_service_has_a_healthcheck() -> None:
    services = full_render()["services"]
    for name, svc in services.items():
        if name in ("jaeger", "mlflow-migrate"):
            # FR-DEPLOY-13: jaeger is distroless (no shell, no probe tool);
            # FR-DEPLOY-15 (g): mlflow-migrate is a one-shot that exits
            assert not svc.get("healthcheck"), name
            continue
        assert (svc.get("healthcheck") or {}).get("test"), name
    expected = {
        "postgres": ["CMD", "pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
        "caddy": ["CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:2019/config/"],
        "embed": ["CMD", "python", "-I", "-m", "reader.search.embed_service", "--healthcheck"],
    } | {role: ["CMD", "reader-server", "--role", role, "--healthcheck"] for role in SERVER_ROLES} | {
        "worker-judge": ["CMD", "reader-server", "--role", "worker", "--healthcheck"],  # FR-DEPLOY-16 (a)
        "agent-proxy": [  # FR-DEPLOY-16 (d): the bound agentnet address, not loopback
            "CMD", "python", "-I", "-c",
            "import socket; socket.create_connection(('10.230.7.2', 8080), 2).close()",
        ],
    }
    for name, test in expected.items():
        assert services[name]["healthcheck"]["test"] == test, name
    for name in ("postgres", "embed", *SERVER_SERVICES):
        health = services[name]["healthcheck"]
        assert {k: health.get(k) for k in HEALTH_TIMING} == HEALTH_TIMING, name


def test_compose_source_has_no_interpolation() -> None:
    assert COMPOSE.exists()
    text = COMPOSE.read_text().replace("$$", "")
    assert "$" not in text, re.findall(r".{0,20}\$.{0,20}", text)


def test_only_server_roles_have_environment() -> None:
    for model in all_renders():
        for name, svc in model["services"].items():
            env = svc.get("environment") or {}
            if name in SERVER_SERVICES:
                assert env == {"READER_CONFIG": "/etc/reader/reader.yaml"}, name
            elif name == "mlflow":  # FR-DEPLOY-15 (a), (g): SRV-OBS FR-OBS-27's six non-secret entries
                assert env == {
                    "MLFLOW_SERVER_ENABLE_JOB_EXECUTION": "false",
                    "MLFLOW_DISABLE_TELEMETRY": "true",
                    "DO_NOT_TRACK": "true",
                    "MLFLOW_ENABLE_AI_GATEWAY": "false",
                    "MLFLOW_ENABLE_REMOTE_ASSISTANT": "false",
                    "MLFLOW_MODEL_CATALOG_URI": "",
                }
            elif name == "rotel":  # FR-DEPLOY-13: its two named exporters (SRV-OBS FR-OBS-19)
                assert set(env) == {
                    "ROTEL_EXPORTER_MLFLOW_ENDPOINT", "ROTEL_EXPORTER_MLFLOW_PROTOCOL",
                    "ROTEL_EXPORTER_MLFLOW_CUSTOM_HEADERS", "ROTEL_EXPORTER_JAEGER_ENDPOINT",
                    "ROTEL_EXPORTER_JAEGER_PROTOCOL",
                }
            elif name == "jaeger":
                assert env == {"GOMEMLIMIT": "200MiB"}
            else:
                assert not env, name


def test_caddyfile_has_no_env_placeholders() -> None:
    assert CADDYFILE.exists()
    text = CADDYFILE.read_text()
    assert "{$" not in text
    assert "{env." not in text
