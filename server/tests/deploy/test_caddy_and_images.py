# @trace FR-DEPLOY-2, FR-DEPLOY-7, FR-DEPLOY-9
"""OPS-DEPLOY [config] Caddyfile text and [image] checks: arm64 manifests, `caddy adapt`, postgres uid."""

import json
from typing import Any

from .conftest import (
    CADDYFILE,
    DEPLOY,
    PROFILES,
    render,
    requires_docker_cli,
    requires_docker_daemon,
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


def _pinned_refs() -> set[str]:
    model = render(PROFILES)
    refs = {svc["image"] for svc in model["services"].values() if "@sha256:" in svc["image"]}
    for path in (DEPLOY / "images").glob("*.Dockerfile"):
        refs |= {ln.split()[1] for ln in path.read_text().splitlines() if ln.upper().startswith("FROM ")}
    return refs


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


@requires_docker_daemon
def test_caddy_adapt_in_pinned_image() -> None:
    caddy_image = render()["services"]["caddy"]["image"]
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
    assert upstreams == {"10.230.1.10:8000", "mcp:8001"}
    assert any(n.get("handler") == "file_server" for n in nodes)
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
