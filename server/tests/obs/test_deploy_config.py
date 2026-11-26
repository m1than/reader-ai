"""SRV-OBS [config]: rotel, mlflow and jaeger in the rendered model, deploy/otel/mlflow.Dockerfile and its catalog
generator, the 8443 and 8444 Caddy sites and the X-Reader-Site headers.

@trace FR-OBS-8 INV-OBS-5 FR-OBS-9 FR-OBS-12 FR-OBS-19
"""

from __future__ import annotations

import json
import re
import subprocess
import sys

import pytest

from deploy.conftest import CADDYFILE, DEPLOY, networks_of, render, requires_docker_cli

ROTEL_IMAGE = (
    "streamfold/rotel:v0.2.7@sha256:7a12878dae3dc494c27def53ebcccbdc18e13389ececf33abf116af3a07c3427"
)
MLFLOW_BASE = (
    "ghcr.io/mlflow/mlflow:v3.18.0@sha256:257204c77980b0f3450a26eb6089e487603fcdd4509529eadaf88f90669f5c13"
)
JAEGER_IMAGE = (
    "jaegertracing/jaeger:2.23.0@sha256:3823829974ccfc2485d2e48b165579091f32295022ae80ca49704e2f4834116e"
)
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
    "font-src 'self' data:; worker-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'"
)
JAEGER_HASHES = (
    "'sha256-eoBUh2b2awnlKCd0fkQUIXAAxPdxWV4aIas48E/3hLU=' 'sha256-07X4W69Q7ST165IvjVZwZU+G34LWjkh2Xj2s9fMwqYQ='"
)
CSP_JAEGER = CSP.replace("script-src 'self';", f"script-src 'self' {JAEGER_HASHES};").replace(
    "base-uri 'none'", "base-uri 'self'"
)
BLOCKED_RE = (  # FR-OBS-9 (code review round 1: any depth, case, repeated slashes)
    "(?i)^/+(?:(?:v1|gateway)(?:/|$)|mcp|(?:ajax-api|api)/+[^/]+/+mlflow/+"
    "(?:assistant|gateway|gateway-proxy)(?:/|$))"
)
OBS = ("rotel", "mlflow", "jaeger")


def _obs() -> dict:  # type: ignore[type-arg]
    return render(("kernels", "agents", "mcp", "observability"))


@requires_docker_cli
def test_services_only_in_the_profile() -> None:
    assert not set(OBS) & set(render(("kernels", "agents", "mcp"))["services"])
    assert set(OBS) <= set(_obs()["services"])


@requires_docker_cli
def test_rotel_service() -> None:
    rotel = _obs()["services"]["rotel"]
    assert rotel["image"] == ROTEL_IMAGE
    # FR-OBS-19: two named exporters, each configured by ROTEL_EXPORTER_<NAME>_<FIELD>.
    assert rotel["command"] == ["--exporters", "mlflow:otlp,jaeger:otlp", "--exporters-traces", "mlflow,jaeger"]
    assert rotel["environment"] == {
        "ROTEL_EXPORTER_MLFLOW_ENDPOINT": "http://mlflow:5000",
        "ROTEL_EXPORTER_MLFLOW_PROTOCOL": "http",
        "ROTEL_EXPORTER_MLFLOW_CUSTOM_HEADERS": "x-mlflow-experiment-id=0",
        "ROTEL_EXPORTER_JAEGER_ENDPOINT": "http://jaeger:4318",
        "ROTEL_EXPORTER_JAEGER_PROTOCOL": "http",
    }
    assert rotel.get("entrypoint") is None
    assert rotel["user"] == "10005:10005"
    assert rotel["healthcheck"]["test"] == ["CMD", "bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/4318"]
    assert int(rotel["mem_limit"]) == int(rotel["memswap_limit"]) == 256 * 1024 * 1024


@requires_docker_cli
def test_mlflow_service() -> None:
    mlflow = _obs()["services"]["mlflow"]
    assert mlflow["image"] == "reader-mlflow:local"
    assert mlflow["build"]["dockerfile"].endswith("deploy/otel/mlflow.Dockerfile")
    assert mlflow["user"] == "10004:10004"
    assert mlflow["environment"] == {  # phase 2: FR-OBS-27 exactly
        "MLFLOW_SERVER_ENABLE_JOB_EXECUTION": "false",
        "MLFLOW_DISABLE_TELEMETRY": "true",
        "DO_NOT_TRACK": "true",
        "MLFLOW_ENABLE_AI_GATEWAY": "false",
        "MLFLOW_ENABLE_REMOTE_ASSISTANT": "false",
        "MLFLOW_MODEL_CATALOG_URI": "",  # FR-OBS-12 (c): no remote price fetch
    }
    assert mlflow["command"] == [  # phase 2: FR-OBS-27 exactly
        "mlflow", "server", "--host", "0.0.0.0", "--port", "5000", "--workers", "1",
        "--backend-store-uri", "sqlite:////var/lib/mlflow/mlflow.db",
        "--artifacts-destination", "/var/lib/mlflow/artifacts", "--default-artifact-root", "mlflow-artifacts:/",
        "--serve-artifacts", "--allowed-hosts", "localhost:8443,mlflow:5000,mlflow", "--x-frame-options", "DENY",
    ]
    assert [v["target"] for v in mlflow["volumes"]] == ["/var/lib/mlflow"]
    assert mlflow["volumes"][0]["source"] == "obs_mlflow"
    assert int(mlflow["mem_limit"]) == int(mlflow["memswap_limit"]) == 1024 * 1024 * 1024
    assert "/health" in " ".join(mlflow["healthcheck"]["test"])


@requires_docker_cli
def test_jaeger_service() -> None:  # FR-OBS-19
    jaeger = _obs()["services"]["jaeger"]
    assert jaeger["image"] == JAEGER_IMAGE
    assert jaeger["command"] == ["--config", "/etc/jaeger/config.yaml"]
    assert jaeger["environment"] == {"GOMEMLIMIT": "200MiB"}
    assert jaeger["user"] == "10006:10006"
    assert int(jaeger["mem_limit"]) == int(jaeger["memswap_limit"]) == 256 * 1024 * 1024
    (config,) = jaeger["volumes"]
    assert config["target"] == "/etc/jaeger/config.yaml" and config["read_only"] is True
    assert config["source"].endswith("deploy/otel/jaeger.yaml")


def test_jaeger_config() -> None:
    from ruamel.yaml import YAML

    conf = YAML(typ="safe").load((DEPLOY / "otel" / "jaeger.yaml").read_text(encoding="utf-8"))
    assert conf["extensions"]["jaeger_storage"]["backends"]["mem"]["memory"]["max_traces"] == 450
    assert conf["receivers"]["otlp"]["protocols"] == {"http": {"endpoint": "0.0.0.0:4318"}}
    assert conf["extensions"]["jaeger_query"]["http"]["endpoint"] == "0.0.0.0:16686"
    assert conf["service"]["telemetry"]["metrics"]["level"] == "none"


@requires_docker_cli
def test_hardening_and_no_ports_or_secrets() -> None:
    services = _obs()["services"]
    for name in OBS:
        svc = services[name]
        assert svc["read_only"] is True, name
        assert svc["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in svc["security_opt"], name
        assert not svc.get("ports"), name
        assert not svc.get("secrets"), name
        assert all(not str(t).startswith("/run/secrets") for t in svc.get("tmpfs") or []), name
        assert any(str(t).startswith("/tmp") for t in svc.get("tmpfs") or []), name


@requires_docker_cli
def test_networks_and_the_volume() -> None:
    model = _obs()
    members: dict[str, set[str]] = {}
    for name, svc in model["services"].items():
        for net in networks_of(svc):
            members.setdefault(net, set()).add(name)
    assert members["otel"] == {"api", "worker", "worker-judge", "rotel"}  # FR-OBS-8 amended (OPS-DEPLOY FR-DEPLOY-16)
    assert members["obs"] == {"rotel", "mlflow", "worker"}
    assert members["obs-ui"] == {"caddy", "mlflow"}
    assert members["jaeger-in"] == {"rotel", "jaeger"}  # FR-OBS-19
    assert members["jaeger-ui"] == {"caddy", "jaeger"}
    names = ("otel", "obs", "obs-ui", "jaeger-in", "jaeger-ui")
    subnets = {n: model["networks"][n]["ipam"]["config"][0]["subnet"] for n in names}
    assert subnets == {"otel": "10.230.2.0/24", "obs": "10.230.3.0/24", "obs-ui": "10.230.4.0/24",
                       "jaeger-in": "10.230.5.0/24", "jaeger-ui": "10.230.6.0/24"}
    assert all(model["networks"][n].get("internal") is True for n in subnets)
    assert networks_of(model["services"]["rotel"]) == {"otel", "obs", "jaeger-in"}
    assert networks_of(model["services"]["mlflow"]) == {"obs", "obs-ui"}
    assert networks_of(model["services"]["jaeger"]) == {"jaeger-in", "jaeger-ui"}
    volume = model["volumes"]["obs_mlflow"]
    assert volume["labels"] == {"org.reader-ai.backup": "include"}  # phase 2: ADR-0038 Decision 6, FR-OBS-30
    users = {n for n, s in model["services"].items() for v in s.get("volumes") or [] if v.get("source") == "obs_mlflow"}
    assert users == {"mlflow", "mlflow-migrate"}  # phase 2: FR-OBS-29
    # The base publishes 443 only; 8443 and 8444 come from `reader up`'s override (OPS-CLI FR-CLI-22).
    assert [p["published"] for p in model["services"]["caddy"]["ports"]] == ["443"]


def test_mlflow_dockerfile_replaces_the_catalog() -> None:  # FR-OBS-12 (c)
    path = DEPLOY / "otel" / "mlflow.Dockerfile"
    lines = [ln.strip() for ln in path.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    assert lines[0] == f"FROM {MLFLOW_BASE}"
    assert lines[1] == "COPY deploy/otel/mlflow-catalog/anthropic.json /tmp/reader-catalog.json"
    run = " ".join(ln.rstrip("\\").strip() for ln in lines[2:-1])
    assert 'rm -f "$P"/*.json' in run and 'cp /tmp/reader-catalog.json "$P/anthropic.json"' in run
    assert "P=/usr/local/lib/python3.11/site-packages/mlflow/utils/model_catalog" in run
    assert "mkdir -p /var/lib/mlflow" in run and "chown 10004:10004 /var/lib/mlflow" in run
    assert lines[-1] == "USER 10004"
    assert not any(ln.upper().startswith("ADD ") for ln in lines)
    assert not any("pip" in ln for ln in lines)  # no driver: the store is SQLite (ADR-0032)
    ignore = DEPLOY / "otel" / "mlflow.Dockerfile.dockerignore"
    assert ignore.read_text().split() == ["**", "!deploy/otel/mlflow-catalog/anthropic.json"]


def test_catalog_generator_check_passes_and_prices_nothing() -> None:  # FR-OBS-12 (c), [unit]
    script = DEPLOY / "otel" / "mlflow_catalog.py"
    done = subprocess.run([sys.executable, str(script), "--check"], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    from ruamel.yaml import YAML

    catalog = json.loads((DEPLOY / "otel" / "mlflow-catalog" / "anthropic.json").read_text(encoding="utf-8"))
    pricing = YAML(typ="safe").load((DEPLOY.parent / "server/src/reader/agent/pricing.yaml").read_text())
    assert set(pricing["models"]) <= set(catalog["models"])
    bundled = {ln.strip() for ln in (DEPLOY / "otel" / "mlflow-bundled-anthropic.txt").read_text().splitlines()
               if ln.strip() and not ln.startswith("#")}
    assert bundled <= set(catalog["models"]) and len(bundled) == 31
    assert catalog["schema_version"] == "1.0"
    assert all(set(v["pricing"].values()) == {0.0} for v in catalog["models"].values())


def _site(host: str) -> str:
    text = CADDYFILE.read_text(encoding="utf-8")
    m = re.search(rf"\n{re.escape(host)} \{{\n(.*?)\n\}}\n?", text, re.S)
    assert m is not None, f"no `{host} {{` site block"
    return m.group(1)


def test_caddyfile_8443_site_text() -> None:
    site = _site("localhost:8443")
    assert "tls internal" in site
    assert "forward_auth 10.230.1.10:8000 {\n\t\turi /api/obs/gate\n\t\theader_up X-Reader-Site mlflow\n\t}" in site
    assert "\t@blocked path_regexp " + BLOCKED_RE + "\n" in site
    assert "handle @blocked {\n\t\trespond 404\n\t}" in site
    proxy = site[site.index("reverse_proxy mlflow:5000") :]
    for line in ("header_up -Cookie", "header_up -Authorization", "header_down -Set-Cookie",
                 "header_down -Access-Control-*"):
        assert line in proxy, line
    assert f'Content-Security-Policy "{CSP}"' in site
    assert site.index("handle @blocked") < site.index("reverse_proxy mlflow:5000")


def test_caddyfile_8444_site_text() -> None:  # FR-OBS-19
    site = _site("localhost:8444")
    assert "tls internal" in site
    assert "forward_auth 10.230.1.10:8000 {\n\t\turi /api/obs/gate\n\t\theader_up X-Reader-Site jaeger\n\t}" in site
    proxy = site[site.index("reverse_proxy jaeger:16686") :]
    for line in ("header_up -Cookie", "header_up -Authorization", "header_down -Set-Cookie",
                 "header_down -Access-Control-*"):
        assert line in proxy, line
    assert f'Content-Security-Policy "{CSP_JAEGER}"' in site
    # Read-only (code review round 1, measured against the pinned image): archive and transform refused for every
    # method, any other non-GET/HEAD method 405, both before the proxy.
    assert "@jaeger_blocked path_regexp (?i)^/+api/+(?:archive|transform)(?:/|$)\n" in site
    assert "handle @jaeger_blocked {\n\t\trespond 404\n\t}" in site
    assert "@jaeger_write not method GET HEAD\n\thandle @jaeger_write {\n\t\trespond 405\n\t}" in site
    assert site.index("handle @jaeger_blocked") < site.index("handle @jaeger_write") < site.index(
        "reverse_proxy jaeger:16686")


@pytest.mark.parametrize("host", ["localhost", "localhost:8443", "localhost:8444"])
def test_every_site_strips_a_client_site_header(host: str) -> None:  # FR-OBS-9 (code review round 1)
    site = _site(host)
    assert "\trequest_header -X-Reader-Site\n" in site


def test_main_site_names_itself_to_the_gate() -> None:  # FR-OBS-9: https://localhost/api/obs/gate
    site = _site("localhost")
    block = "handle /api/obs/gate {\n\t\treverse_proxy 10.230.1.10:8000 {\n\t\t\theader_up X-Reader-Site app\n\t\t}"
    assert block in site
    assert site.index("handle /api/obs/gate") < site.index("handle /api/* {")
