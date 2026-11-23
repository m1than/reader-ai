"""SRV-OBS [config]: rotel and mlflow in the rendered model, deploy/otel/mlflow.Dockerfile, the 8443 Caddy site.

@trace FR-OBS-8 INV-OBS-5 FR-OBS-9
"""

from __future__ import annotations

import re

from deploy.conftest import CADDYFILE, DEPLOY, networks_of, render, requires_docker_cli

pytestmark = requires_docker_cli

ROTEL_IMAGE = (
    "streamfold/rotel:v0.2.7@sha256:7a12878dae3dc494c27def53ebcccbdc18e13389ececf33abf116af3a07c3427"
)
MLFLOW_BASE = (
    "ghcr.io/mlflow/mlflow:v3.18.0@sha256:257204c77980b0f3450a26eb6089e487603fcdd4509529eadaf88f90669f5c13"
)
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
    "font-src 'self' data:; worker-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'"
)
BLOCKED = (
    "/v1/*",
    "/gateway/*",
    "/ajax-api/*/mlflow/assistant/*",
    "/ajax-api/*/mlflow/gateway-proxy*",
    "/mcp*",
    "/api/*/mlflow/gateway/*",
)


def _obs() -> dict:  # type: ignore[type-arg]
    return render(("kernels", "agents", "mcp", "observability"))


def test_services_only_in_the_profile() -> None:
    assert not {"rotel", "mlflow"} & set(render(("kernels", "agents", "mcp"))["services"])
    services = _obs()["services"]
    assert {"rotel", "mlflow"} <= set(services)


def test_rotel_service() -> None:
    rotel = _obs()["services"]["rotel"]
    assert rotel["image"] == ROTEL_IMAGE
    assert rotel["command"] == [
        "--exporter",
        "otlp",
        "--otlp-exporter-endpoint",
        "http://mlflow:5000",
        "--otlp-exporter-protocol",
        "http",
        "--otlp-exporter-custom-headers",
        "x-mlflow-experiment-id=0",
    ]
    assert rotel.get("entrypoint") is None
    assert rotel["user"] == "10005:10005"
    assert rotel["healthcheck"]["test"] == ["CMD", "bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/4318"]
    assert int(rotel["mem_limit"]) == int(rotel["memswap_limit"]) == 256 * 1024 * 1024


def test_mlflow_service() -> None:
    mlflow = _obs()["services"]["mlflow"]
    assert mlflow["image"] == "reader-mlflow:local"
    assert mlflow["build"]["dockerfile"].endswith("deploy/otel/mlflow.Dockerfile")
    assert mlflow["user"] == "10004:10004"
    assert mlflow["environment"] == {
        "MLFLOW_SERVER_ENABLE_JOB_EXECUTION": "false",
        "MLFLOW_DISABLE_TELEMETRY": "true",
    }
    assert mlflow["command"] == [
        "mlflow",
        "server",
        "--host",
        "0.0.0.0",
        "--port",
        "5000",
        "--workers",
        "1",
        "--backend-store-uri",
        "sqlite:////var/lib/mlflow/mlflow.db",
        "--artifacts-destination",
        "/var/lib/mlflow/artifacts",
        "--default-artifact-root",
        "mlflow-artifacts:/",
        "--allowed-hosts",
        "localhost:8443,mlflow:5000,mlflow",
    ]
    assert [v["target"] for v in mlflow["volumes"]] == ["/var/lib/mlflow"]
    assert mlflow["volumes"][0]["source"] == "obs_mlflow"
    assert int(mlflow["mem_limit"]) == int(mlflow["memswap_limit"]) == 1024 * 1024 * 1024
    assert "/health" in " ".join(mlflow["healthcheck"]["test"])


def test_hardening_and_no_ports_or_secrets() -> None:
    services = _obs()["services"]
    for name in ("rotel", "mlflow"):
        svc = services[name]
        assert svc["read_only"] is True, name
        assert svc["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in svc["security_opt"], name
        assert not svc.get("ports"), name
        assert not svc.get("secrets"), name
        assert all(not str(t).startswith("/run/secrets") for t in svc.get("tmpfs") or []), name
        assert any(str(t).startswith("/tmp") for t in svc.get("tmpfs") or []), name


def test_networks_and_the_volume() -> None:
    model = _obs()
    members: dict[str, set[str]] = {}
    for name, svc in model["services"].items():
        for net in networks_of(svc):
            members.setdefault(net, set()).add(name)
    assert members["otel"] == {"api", "worker", "rotel"}
    assert members["obs"] == {"rotel", "mlflow", "worker"}
    assert members["obs-ui"] == {"caddy", "mlflow"}
    subnets = {n: model["networks"][n]["ipam"]["config"][0]["subnet"] for n in ("otel", "obs", "obs-ui")}
    assert subnets == {"otel": "10.230.2.0/24", "obs": "10.230.3.0/24", "obs-ui": "10.230.4.0/24"}
    assert all(model["networks"][n].get("internal") is True for n in subnets)
    assert networks_of(model["services"]["rotel"]) == {"otel", "obs"}
    assert networks_of(model["services"]["mlflow"]) == {"obs", "obs-ui"}
    volume = model["volumes"]["obs_mlflow"]
    assert volume["labels"] == {"org.reader-ai.backup": "exclude"}
    users = [
        n
        for n, s in model["services"].items()
        for v in s.get("volumes") or []
        if v.get("source") == "obs_mlflow"
    ]
    assert users == ["mlflow"]
    # The base publishes 443 only; 8443 comes from `reader up`'s override (OPS-CLI FR-CLI-22).
    assert [p["published"] for p in model["services"]["caddy"]["ports"]] == ["443"]


def test_mlflow_dockerfile() -> None:
    path = DEPLOY / "otel" / "mlflow.Dockerfile"
    assert path.exists(), "deploy/otel/mlflow.Dockerfile does not exist"
    lines = [ln.strip() for ln in path.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    assert lines[0] == f"FROM {MLFLOW_BASE}"
    assert any("mkdir -p /var/lib/mlflow" in ln and "chown 10004:10004 /var/lib/mlflow" in ln for ln in lines)
    assert lines[-1] == "USER 10004"
    assert not any(ln.upper().startswith(("ADD ", "COPY ")) for ln in lines)
    assert not any("pip" in ln for ln in lines)  # no driver: the store is SQLite (ADR-0032)
    ignore = DEPLOY / "otel" / "mlflow.Dockerfile.dockerignore"
    assert ignore.exists() and ignore.read_text().split() == ["**"]


def test_caddyfile_8443_site_text() -> None:
    text = CADDYFILE.read_text(encoding="utf-8")
    m = re.search(r"\nlocalhost:8443 \{\n(.*?)\n\}\n", text, re.S)
    assert m is not None, "no `localhost:8443 {` site block"
    site = m.group(1)
    assert "tls internal" in site
    assert "forward_auth 10.230.1.10:8000 {\n\t\turi /api/obs/gate\n\t}" in site
    assert "@blocked path " + " ".join(BLOCKED) in site
    assert "handle @blocked {\n\t\trespond 404\n\t}" in site
    proxy = site[site.index("reverse_proxy mlflow:5000") :]
    for line in (
        "header_up -Cookie",
        "header_up -Authorization",
        "header_down -Set-Cookie",
        "header_down -Access-Control-*",
    ):
        assert line in proxy, line
    assert f'Content-Security-Policy "{CSP}"' in site
    assert site.index("handle @blocked") < site.index("reverse_proxy mlflow:5000")
