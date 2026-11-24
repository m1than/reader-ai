# @trace FR-DEPLOY-2, FR-DEPLOY-7, FR-DEPLOY-9, FR-DEPLOY-13, FR-DEPLOY-14
"""OPS-DEPLOY [config] Caddyfile text and the derived caddy image, [image] checks (`caddy adapt`, postgres
uid), the [image: registry] check (arm64 manifests; only with READER_TEST_REGISTRY=1) and the [image: build]
check of reader-caddy:local (only with READER_TEST_IMAGE_BUILD=1)."""

import json
import os
from typing import Any

import pytest

from .conftest import (
    CADDYFILE,
    DEPLOY,
    PROFILES,
    registry,
    render,
    requires_docker_cli,
    requires_docker_daemon,
    requires_registry_opt_in,
    run_ok,
)

HEADERS = {
    "Strict-Transport-Security", "Content-Security-Policy", "X-Content-Type-Options", "Referrer-Policy",
}


@requires_docker_cli
def test_caddyfile_text() -> None:
    assert CADDYFILE.exists()
    text = CADDYFILE.read_text()
    assert "localhost {" in text
    assert "tls internal" in text
    assert "admin localhost:2019" in text
    assert "frame-ancestors 'none'" in text
    assert all(h in text for h in HEADERS)
    assert f'Content-Security-Policy "{APP_CSP}"' in text
    # The app's site only: the 8443 dashboard site has its own measured CSP (FR-DEPLOY-13, SRV-OBS FR-OBS-9).
    app_site = text.split("\nlocalhost:8443 {", 1)[0]
    assert "data:" not in app_site
    assert "unsafe-inline" not in app_site
    assert "try_files {path} /index.html" in text
    # Assets are files only: their handler has a file server and no fallback to the SPA index.
    assets = text.split("handle /assets/* {", 1)[1].split("}", 1)[0] if "handle /assets/* {" in text else ""
    assert "file_server" in assets
    assert "try_files" not in assets


# WEB-UI's app CSP (V.OPS-DEPLOY-web, FR-DEPLOY-7).
APP_CSP = (
    "default-src 'self'; img-src 'self' blob:; connect-src 'self' wss://localhost; frame-src 'none'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
CADDY_DOCKERFILE = DEPLOY / "images" / "caddy.Dockerfile"


def _caddy_runtime_image() -> str:
    """The pinned caddy image of caddy.Dockerfile's runtime (last) stage."""
    assert CADDY_DOCKERFILE.exists(), CADDY_DOCKERFILE
    lines = CADDY_DOCKERFILE.read_text().splitlines()
    froms = [ln.split()[1] for ln in lines if ln.upper().startswith("FROM ")]
    assert froms, "caddy.Dockerfile has no FROM"
    return froms[-1]


@requires_docker_cli
def test_caddy_service_is_the_derived_image() -> None:
    caddy = render()["services"]["caddy"]
    assert caddy["image"] == "reader-caddy:local"
    assert caddy["build"]["dockerfile"] == "deploy/images/caddy.Dockerfile"
    assert _caddy_runtime_image().startswith("caddy:2-alpine@sha256:")
    text = CADDY_DOCKERFILE.read_text()
    assert "COPY --from=web /web/dist /srv/spa" in text
    assert "RUN npx vite build" in text


requires_image_build_opt_in = pytest.mark.skipif(
    os.environ.get("READER_TEST_IMAGE_BUILD") != "1", reason="image build; set READER_TEST_IMAGE_BUILD=1",
)


@requires_docker_daemon
@requires_image_build_opt_in
def test_caddy_image_carries_the_spa() -> None:
    dockerfile, context = str(CADDY_DOCKERFILE), str(DEPLOY.parent)
    run_ok("docker", "build", "-q", "-f", dockerfile, "-t", "reader-caddy:test", context)
    try:
        listing = run_ok(
            "docker", "run", "--rm", "--network", "none", "--entrypoint", "find", "reader-caddy:test",
            "/srv/spa", "-type", "f",
        )
    finally:
        run_ok("docker", "image", "rm", "reader-caddy:test")
    files = listing.split()
    assert "/srv/spa/index.html" in files
    assert any(f.startswith("/srv/spa/assets/") and f.endswith(".js") for f in files)
    assert not any("/.env" in f for f in files)


def _pinned_refs() -> set[str]:
    model = render(PROFILES)
    refs = {svc["image"] for svc in model["services"].values() if "@sha256:" in svc["image"]}
    for path in (DEPLOY / "images").glob("*.Dockerfile"):
        refs |= {ln.split()[1] for ln in path.read_text().splitlines() if ln.upper().startswith("FROM ")}
    return refs


@registry
@requires_registry_opt_in
@requires_docker_daemon
def test_pinned_images_offer_arm64() -> None:
    refs = _pinned_refs()
    assert refs
    for ref in refs:
        out = run_ok("docker", "buildx", "imagetools", "inspect", ref, "--format", "{{json .Manifest}}")
        manifests = json.loads(out).get("manifests", [])
        platforms = {(m.get("platform") or {}).get("architecture") for m in manifests}
        assert "arm64" in platforms, (ref, platforms)


def _walk(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        found.append(node)
        for value in node.values():
            found += _walk(value)
    elif isinstance(node, list):
        for value in node:
            found += _walk(value)
    return found


def _path_upstreams(nodes: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Each route with a path matcher → the upstreams of the reverse proxies inside its handlers."""
    mapping: dict[str, set[str]] = {}
    for route in nodes:
        paths = [p for m in route.get("match") or [] if isinstance(m, dict) for p in m.get("path", [])]
        dials = {
            u["dial"]
            for n in _walk(route.get("handle", []))
            if n.get("handler") == "reverse_proxy"
            for u in n.get("upstreams", [])
        }
        for path in paths:
            mapping.setdefault(path, set()).update(dials)
    return mapping


BLOCKED_RE = (  # SRV-OBS FR-OBS-9, exactly
    "(?i)^/+(?:(?:v1|gateway)(?:/|$)|mcp|(?:ajax-api|api)/+[^/]+/+mlflow/+"
    "(?:assistant|gateway|gateway-proxy)(?:/|$))"
)
# Code review round 1: deep paths, case and repeated slashes (Go's RE2 and Python agree on this regexp).
BLOCKED_PATHS = [
    "/v1/traces", "/v1", "/gateway/x", "/mcp", "/mcp/x",
    "/api/3.0/mlflow/gateway/secrets/create", "/api/3.0/mlflow/gateway/endpoints/create",
    "/ajax-api/3.0/mlflow/gateway/endpoints/create",
    "/ajax-api/3.0/mlflow/assistant/sessions/abc/stream",
    "/ajax-api/3.0/mlflow/assistant/providers/claude_code/health",
    "/ajax-api/2.0/mlflow/gateway-proxy/v1/chat", "/ajax-api/2.0/mlflow/gateway-proxy",
    "//api/3.0/mlflow/gateway/secrets/create", "/api//3.0//mlflow//gateway/x",
    "/AJAX-API/3.0/MLFLOW/Assistant/x", "/API/3.0/mlflow/GATEWAY/secrets/create", "/V1/traces",
]
OPEN_PATHS = [
    "/", "/api/2.0/mlflow/experiments/search", "/ajax-api/3.0/mlflow/traces/get", "/static-files/x.js",
    "/api/3.0/mlflow/gateways", "/v10/x",
]


@requires_docker_cli
def test_blocked_regexp_decides_the_paths() -> None:
    """FR-OBS-9's regexp on its own; the [image] edge test sends the same paths through the pinned Caddy."""
    import re

    text = CADDYFILE.read_text(encoding="utf-8")
    assert f"\t@blocked path_regexp {BLOCKED_RE}\n" in text
    blocked = re.compile(BLOCKED_RE)
    assert [p for p in BLOCKED_PATHS if not blocked.search(p)] == []
    assert [p for p in OPEN_PATHS if blocked.search(p)] == []


def _check_dashboard_site(config: dict[str, Any]) -> None:
    """FR-DEPLOY-13 / SRV-OBS FR-OBS-9: on :8443 the gate runs first, then the blocked paths answer Caddy's
    own 404 in a `handle` placed before the MLflow proxy, which strips cookies, credentials, CORS headers."""
    servers = config["apps"]["http"]["servers"]
    (site,) = [s for s in servers.values() if ":8443" in s.get("listen", [])]
    text = json.dumps(site)
    assert '"uri": "/api/obs/gate' in text.replace("?", "")  # forward_auth's rewrite to the gate
    order = [text.index(marker) for marker in ('"10.230.1.10:8000"', '"static_response"', '"mlflow:5000"')]
    assert order == sorted(order), "gate, then the blocked 404, then the MLflow proxy"
    nodes = _walk(site)
    patterns = [m["path_regexp"]["pattern"] for n in nodes for m in n.get("match", []) if "path_regexp" in m]
    assert patterns == [BLOCKED_RE]
    (blocked,) = [n for n in nodes if n.get("handler") == "static_response"]
    assert blocked.get("status_code") in (404, "404")
    (mlflow,) = [n for n in nodes if n.get("handler") == "reverse_proxy"
                 and any(u.get("dial") == "mlflow:5000" for u in n.get("upstreams", []))]
    headers = mlflow.get("headers", {})
    assert set(headers["request"]["delete"]) == {"Cookie", "Authorization"}
    assert set(headers["response"]["delete"]) == {"Set-Cookie", "Access-Control-*"}


@requires_docker_daemon
def test_caddy_adapt_in_pinned_image() -> None:
    caddy_image = _caddy_runtime_image()
    out = run_ok(
        "docker", "run", "--rm", "--network", "none",
        "-v", f"{CADDYFILE}:/etc/caddy/Caddyfile:ro",
        caddy_image, "caddy", "adapt", "--config", "/etc/caddy/Caddyfile",
    )
    config = json.loads(out)
    assert config["admin"]["listen"] == "localhost:2019"
    nodes = _walk(config)
    handlers = {n.get("handler") for n in nodes if "handler" in n}
    assert "forward_proxy" not in handlers
    proxies = [n for n in nodes if n.get("handler") == "reverse_proxy"]
    upstreams = {u["dial"] for n in proxies for u in n.get("upstreams", [])}
    # FR-DEPLOY-13 (paired SRV-OBS change): the 8443 site adds forward_auth to the api and the MLflow proxy;
    # the 8444 site (SRV-OBS FR-OBS-19) the Jaeger proxy.
    assert upstreams == {"10.230.1.10:8000", "mcp:8001", "mlflow:5000", "jaeger:16686"}
    proxied = {path: ups for path, ups in _path_upstreams(nodes).items() if ups}
    assert proxied == {
        "/api/materials/uploads": {"10.230.1.10:8000"},  # FR-DEPLOY-14: the upload's own body limit
        "/api/obs/gate": {"10.230.1.10:8000"},  # FR-DEPLOY-13 (SRV-OBS FR-OBS-9): X-Reader-Site app
        "/api/*": {"10.230.1.10:8000"},
        "/mcp": {"mcp:8001"},
    }
    _check_dashboard_site(config)
    assert any(n.get("handler") == "file_server" for n in nodes)
    assert "/index.html" in json.dumps(config), "SPA fallback (try_files) missing"
    assert '"/assets/*"' in json.dumps(config), "assets route missing"
    issuers = {i.get("module") for n in nodes for i in n.get("issuers", []) if isinstance(i, dict)}
    assert "internal" in issuers


@requires_docker_daemon
def test_postgres_base_image_uid_999() -> None:
    path = DEPLOY / "images" / "postgres.Dockerfile"
    assert path.exists(), "deploy/images/postgres.Dockerfile does not exist"
    dockerfile = path.read_text()
    base = next(ln.split()[1] for ln in dockerfile.splitlines() if ln.upper().startswith("FROM "))
    passwd = run_ok("docker", "run", "--rm", "--network", "none", "--entrypoint", "cat", base, "/etc/passwd")
    assert any(line.startswith("postgres:x:999:999:") for line in passwd.splitlines())
