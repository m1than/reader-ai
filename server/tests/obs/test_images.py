"""SRV-OBS [image] (opt-in, READER_TEST_IMAGE_BUILD=1): the CLI canary, Rotel → MLflow under the compose hardening,
retention against real MLflow, and the 8443 edge in front of a stub gate and an echo upstream.

@trace INV-OBS-2 FR-OBS-6 FR-OBS-8 FR-OBS-11 INV-OBS-5 NFR-OBS-1

Every container runs under a per-test name prefix on per-test internal networks; nothing publishes a port. The CLI
canary harness lives in cli_canary/ (moved from spikes/cli-otel-content); its image doubles as the client that sends
spans and HTTP requests from inside the networks. Zero credit: no real token, a stub upstream.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from .conftest import FIXTURES, REPO, load_yaml, require

pytestmark = pytest.mark.skipif(
    os.environ.get("READER_TEST_IMAGE_BUILD") != "1" or shutil.which("docker") is None,
    reason="image test; set READER_TEST_IMAGE_BUILD=1 (builds images, pulls MLflow, Rotel and Caddy)",
)

CANARY_DIR = Path(__file__).resolve().parent / "cli_canary"
CANARY_IMAGE = "reader-test-cli-canary:local"
MLFLOW_IMAGE = "reader-mlflow:local"
ROTEL = "streamfold/rotel:v0.2.7@sha256:7a12878dae3dc494c27def53ebcccbdc18e13389ececf33abf116af3a07c3427"
CADDY = "caddy:2-alpine@sha256:003a97808e781aa9f7e75cc2f43d34d461099031faf5e20faefb9f61126836f0"
HARDEN = ["--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]


def sh(*cmd: str, check: bool = True, timeout: float = 600) -> str:
    proc = subprocess.run(list(cmd), capture_output=True, text=True, check=False, timeout=timeout)
    if check:
        assert proc.returncode == 0, proc.stderr[-2000:]
    return proc.stdout


def _sdk_pin() -> str:
    lock = (REPO / "server" / "uv.lock").read_text(encoding="utf-8")
    match = re.search(r'\[\[package\]\]\nname = "claude-agent-sdk"\nversion = "([^"]+)"', lock)
    assert match is not None
    return match.group(1)


@pytest.fixture(scope="module")
def canary_image() -> str:
    sh(
        "docker",
        "build",
        "-q",
        "--build-arg",
        f"CLAUDE_AGENT_SDK={_sdk_pin()}",
        "-t",
        CANARY_IMAGE,
        str(CANARY_DIR),
    )
    return CANARY_IMAGE


@pytest.fixture(scope="module")
def mlflow_image() -> str:
    sh(
        "docker",
        "build",
        "-q",
        "-f",
        str(REPO / "deploy/otel/mlflow.Dockerfile"),
        "-t",
        MLFLOW_IMAGE,
        str(REPO / "deploy/otel"),
    )
    return MLFLOW_IMAGE


@pytest.fixture
def prefix() -> Iterator[str]:
    name = "reader-test-obs-" + secrets.token_hex(4)
    yield name
    ids = sh("docker", "ps", "-aq", "--filter", f"name={name}", check=False).split()
    if ids:
        sh("docker", "rm", "-f", *ids, check=False)
    for net in sh("docker", "network", "ls", "-q", "--filter", f"name={name}", check=False).split():
        sh("docker", "network", "rm", net, check=False)
    for vol in sh("docker", "volume", "ls", "-q", "--filter", f"name={name}", check=False).split():
        sh("docker", "volume", "rm", vol, check=False)


def test_cli_canary(canary_image: str, tmp_path: Path) -> None:
    cli_env = require("reader.obs.cli_env")
    additions = dict(cli_env.cli_env_additions())
    additions["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://127.0.0.1:4318"  # the harness's local receiver
    out = tmp_path / "out"
    out.mkdir()
    out.chmod(0o777)
    canaries = {
        k: f"{k.lower()}{secrets.token_hex(8)}" for k in ("CANARY_PROMPT", "CANARY_SYSTEM", "CANARY_RESPONSE")
    }
    handle = secrets.token_urlsafe(32)
    env = [
        arg
        for k, v in {**canaries, "HANDLE": handle, "OBS_ENV": json.dumps(additions)}.items()
        for arg in ("-e", f"{k}={v}")
    ]
    sh(
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        *HARDEN,
        "--tmpfs",
        "/home/worker:uid=1000,size=64m",
        "--tmpfs",
        "/tmp:uid=1000,size=64m",
        "-e",
        "HOME=/home/worker",
        *env,
        "-v",
        f"{out}:/out",
        canary_image,
        timeout=300,
    )
    report = json.loads((out / "report.json").read_text())["modes"]
    vec = load_yaml(FIXTURES / "cli-span-keys.yaml")
    plain = report["agent-env"]
    assert plain["error"] is None
    assert plain["hits"] == {"CANARY_PROMPT": 0, "CANARY_SYSTEM": 0, "CANARY_RESPONSE": 0, "HANDLE": 0}
    assert plain["authorization_present"] is False
    assert plain["other_hosts"] == []
    assert plain["cli_spans"], "the CLI exported no span"
    assert plain["under_agent_call"] == ["claude_code.interaction"]
    for span in plain["cli_spans"]:
        assert span["name"] in vec["spans"], span["name"]
        assert set(span["keys"]) <= set(vec["spans"][span["name"]]["keys"]), span["name"]
        assert set(span["resource_keys"]) <= set(vec["resource_keys"])
    assert report["positive-control"]["hits"]["CANARY_PROMPT"] >= 1  # the scan works


def _run(
    prefix: str, name: str, image: str, *args: str, nets: tuple[str, ...], opts: tuple[str, ...] = ()
) -> None:
    sh(
        "docker",
        "run",
        "-d",
        "--name",
        f"{prefix}-{name}",
        "--network",
        f"{prefix}-{nets[0]}",
        "--network-alias",
        name,
        *opts,
        image,
        *args,
    )
    for net in nets[1:]:
        sh("docker", "network", "connect", "--alias", name, f"{prefix}-{net}", f"{prefix}-{name}")


def _client(prefix: str, canary_image: str, net: str, script: str, timeout: float = 120) -> str:
    return sh(
        "docker",
        "run",
        "--rm",
        "--network",
        f"{prefix}-{net}",
        "--entrypoint",
        "python",
        canary_image,
        "-c",
        script,
        timeout=timeout,
    )


def _health(command: str) -> tuple[str, ...]:
    """Docker's own healthcheck running the compose test's command (no `docker exec` from the test)."""
    return (
        "--health-cmd",
        command,
        "--health-interval",
        "2s",
        "--health-timeout",
        "2s",
        "--health-retries",
        "3",
    )


MLFLOW_HEALTH = (
    'python -c "import urllib.request,sys; '
    "sys.exit(urllib.request.urlopen('http://127.0.0.1:5000/health',timeout=2).status!=200)\""
)
ROTEL_HEALTH = "bash -c 'exec 3<>/dev/tcp/127.0.0.1/4318'"


def _wait_healthy(prefix: str, name: str, limit: float = 60) -> float:
    started = time.monotonic()
    while time.monotonic() - started < limit:
        status = sh(
            "docker", "inspect", "--format", "{{.State.Health.Status}}", f"{prefix}-{name}", check=False
        )
        if status.strip() == "healthy":
            return time.monotonic() - started
        time.sleep(1)
    raise AssertionError(f"{name} not healthy within {limit} s")


def _backend(prefix: str, mlflow_image: str) -> None:
    for net in ("otel", "obs"):
        sh("docker", "network", "create", "--internal", f"{prefix}-{net}")
    _run(
        prefix,
        "mlflow",
        mlflow_image,
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
        nets=("obs",),
        opts=(
            "--user",
            "10004:10004",
            *HARDEN,
            "--tmpfs",
            "/tmp:size=64m,uid=10004",
            "-e",
            "MLFLOW_SERVER_ENABLE_JOB_EXECUTION=false",
            "-e",
            "MLFLOW_DISABLE_TELEMETRY=true",
            "-v",
            f"{prefix}-data:/var/lib/mlflow",
            "--memory",
            "1g",
            "--memory-swap",
            "1g",
            *_health(MLFLOW_HEALTH),
        ),
    )
    _run(
        prefix,
        "rotel",
        ROTEL,
        "--exporter",
        "otlp",
        "--otlp-exporter-endpoint",
        "http://mlflow:5000",
        "--otlp-exporter-protocol",
        "http",
        "--otlp-exporter-custom-headers",
        "x-mlflow-experiment-id=0",
        nets=("otel", "obs"),
        opts=(
            "--user",
            "10005:10005",
            *HARDEN,
            "--tmpfs",
            "/tmp:size=16m,uid=10005",
            "--memory",
            "256m",
            "--memory-swap",
            "256m",
            *_health(ROTEL_HEALTH),
        ),
    )
    _wait_healthy(prefix, "mlflow")
    _wait_healthy(prefix, "rotel")


SEND = """
import json, sys, time
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
p = TracerProvider()
p.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint="http://rotel:4318/v1/traces")))
t = p.get_tracer("reader")
ago = int(sys.argv[1]) if len(sys.argv) > 1 else 0
start = time.time_ns() - ago * 86400 * 10**9
root = t.start_span("job tutor.answer", start_time=start)
from opentelemetry import trace
with trace.use_span(root):
    child = t.start_span("agent.call", start_time=start)
    child.end(end_time=start + 10**6)
root.end(end_time=start + 2 * 10**6)
p.shutdown()
print(json.dumps({"trace": format(root.get_span_context().trace_id, "032x"), "ended": time.time()}))
"""

GET = """
import json, sys, time, urllib.request, urllib.error
deadline = time.time() + 30
while True:
    try:
        url = "http://mlflow:5000/api/3.0/mlflow/traces/get?trace_id=tr-" + sys.argv[1]
        with urllib.request.urlopen(url, timeout=5) as r:
            body = json.load(r)
        spans = body.get("trace", {}).get("data", {}).get("spans", [])
        if len(spans) >= int(sys.argv[2]):
            print(json.dumps({"found": time.time(), "spans": len(spans)})); break
    except urllib.error.HTTPError:
        pass
    if time.time() > deadline:
        print(json.dumps({"found": None})); break
    time.sleep(1)
"""


def _send(prefix: str, image: str, days_ago: int = 0) -> dict[str, Any]:
    return json.loads(
        _client(
            prefix,
            image,
            "otel",
            SEND.replace("sys.argv[1]", str(days_ago)).replace("len(sys.argv) > 1", "True"),
        )
    )


def _get(prefix: str, image: str, trace: str, spans: int = 2) -> dict[str, Any]:
    script = GET.replace("sys.argv[1]", repr(trace)).replace("int(sys.argv[2])", str(spans))
    return json.loads(_client(prefix, image, "obs", script))


def test_backend_ingest_within_nfr(prefix: str, canary_image: str, mlflow_image: str) -> None:
    _backend(prefix, mlflow_image)
    sent = _send(prefix, canary_image)
    got = _get(prefix, canary_image, sent["trace"])
    assert got["found"] is not None, "the trace never reached MLflow"
    assert got["spans"] == 2
    assert got["found"] - sent["ended"] <= 30  # NFR-OBS-1


def test_retention_against_real_mlflow(prefix: str, canary_image: str, mlflow_image: str) -> None:
    jobs = require("reader.obs.jobs")
    _backend(prefix, mlflow_image)
    old = _send(prefix, canary_image, days_ago=40)
    fresh = _send(prefix, canary_image)
    assert _get(prefix, canary_image, old["trace"])["found"] is not None
    assert _get(prefix, canary_image, fresh["trace"])["found"] is not None
    url, body = jobs.delete_request(time.time(), 30)
    script = (
        "import json, urllib.request\n"
        f"req = urllib.request.Request({url!r}, data={json.dumps(body).encode()!r}, method='POST',"
        " headers={'Content-Type': 'application/json'})\n"
        "print(urllib.request.urlopen(req, timeout=30).read().decode())\n"
    )
    assert json.loads(_client(prefix, canary_image, "obs", script))["traces_deleted"] == 1
    probe = GET.replace("deadline = time.time() + 30", "deadline = time.time() + 3")
    script_old = probe.replace("sys.argv[1]", repr(old["trace"])).replace("int(sys.argv[2])", "1")
    assert json.loads(_client(prefix, canary_image, "obs", script_old))["found"] is None
    assert _get(prefix, canary_image, fresh["trace"])["found"] is not None


GATE = """
import http.server, json, threading
class G(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        import sys; sys.path.insert(0, "/src")
        from reader.obs.gate_rules import decide
        role = "owner" if "__Host-reader_session=owner" in self.headers.get("Cookie", "") else None
        h = self.headers
        status = decide(h.get("Origin"), h.get("X-Forwarded-Method"), h.get("Sec-Fetch-Site"),
                        h.get("Sec-Fetch-Mode"), h.get("Sec-Fetch-Dest"), role)
        self.send_response(status); self.send_header("Content-Length", "0"); self.end_headers()
class E(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def handle_one(self):
        body = json.dumps(sorted(k.lower() for k in self.headers.keys())).encode()
        self.send_response(200); self.send_header("Set-Cookie", "x=1")
        self.send_header("Access-Control-Allow-Origin", "https://localhost:3000")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    do_GET = handle_one
    do_POST = handle_one
for port, cls in ((9000, G), (5000, E)):
    threading.Thread(target=http.server.ThreadingHTTPServer(("0.0.0.0", port), cls).serve_forever).start()
"""


def _edge_caddyfile() -> str:
    """deploy/Caddyfile's 8443 site, served on plain :8443 against the stub gate and the echo upstream."""
    text = (REPO / "deploy/Caddyfile").read_text(encoding="utf-8")
    site = re.search(r"\nlocalhost:8443 \{\n(.*?)\n\}\n", text, re.S)
    assert site is not None
    body = site.group(1).replace("\ttls internal\n", "").replace("10.230.1.10:8000", "gate:9000")
    return "{\n\tadmin off\n\tauto_https off\n}\n\n:8443 {\n" + body + "\n}\n"


def test_edge_gate_and_header_stripping(prefix: str, canary_image: str, tmp_path: Path) -> None:
    (tmp_path / "Caddyfile").write_text(_edge_caddyfile(), encoding="utf-8")
    (tmp_path / "stubs.py").write_text(GATE, encoding="utf-8")
    sh("docker", "network", "create", "--internal", f"{prefix}-edge")
    src = REPO / "server" / "src"
    _run(
        prefix,
        "stubs",
        canary_image,
        "/s/stubs.py",
        nets=("edge",),
        opts=(
            "--entrypoint",
            "python",
            "-v",
            f"{tmp_path}:/s:ro",
            "-v",
            f"{src}:/src:ro",
            "--network-alias",
            "gate",
            "--network-alias",
            "mlflow",
        ),
    )
    _run(
        prefix,
        "caddy",
        CADDY,
        nets=("edge",),
        opts=(
            "-v",
            f"{tmp_path / 'Caddyfile'}:/etc/caddy/Caddyfile:ro",
        ),
    )
    time.sleep(3)
    probe = """
import json, urllib.request, urllib.error
def req(path, cookie=None, origin=None, method="GET", site=None, auth=False):
    r = urllib.request.Request("http://caddy:8443" + path, method=method, data=b"{}" if method != "GET" else None)
    r.add_header("Host", "localhost:8443")
    if cookie: r.add_header("Cookie", "__Host-reader_session=" + cookie)
    if origin: r.add_header("Origin", origin)
    if site: r.add_header("Sec-Fetch-Site", site); r.add_header("Sec-Fetch-Mode", "no-cors"); r.add_header("Sec-Fetch-Dest", "image")
    if auth: r.add_header("Authorization", "Bearer x")
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return {"status": resp.status, "body": resp.read().decode(), "headers": dict(resp.headers)}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "body": e.read().decode(), "headers": dict(e.headers)}
out = {
  "none": req("/"), "owner": req("/", "owner", auth=True),
  "post_foreign": req("/x", "owner", "https://localhost:3000", "POST"),
  "post_app": req("/x", "owner", "https://localhost", "POST"),
  "post_none": req("/x", "owner", None, "POST"),
  "post_dash": req("/x", "owner", "https://localhost:8443", "POST"),
  "img_same_site": req("/x", "owner", site="same-site"),
}
for p in ["/v1/traces", "/gateway/x", "/ajax-api/3.0/mlflow/assistant/x", "/ajax-api/2.0/mlflow/gateway-proxy",
          "/mcp", "/api/2.0/mlflow/gateway/x"]:
    out[p] = req(p, "owner")
print(json.dumps(out))
"""
    out = json.loads(_client(prefix, canary_image, "edge", probe))
    assert out["none"]["status"] == 401
    owner = out["owner"]
    assert owner["status"] == 200
    upstream_saw = json.loads(owner["body"])
    assert "cookie" not in upstream_saw and "authorization" not in upstream_saw
    assert not any(
        k.lower().startswith("access-control-") or k.lower() == "set-cookie" for k in owner["headers"]
    )
    for case in ("post_foreign", "post_app", "post_none", "img_same_site"):
        assert out[case]["status"] == 403, case
    assert out["post_dash"]["status"] == 200
    for path in [
        "/v1/traces",
        "/gateway/x",
        "/ajax-api/3.0/mlflow/assistant/x",
        "/ajax-api/2.0/mlflow/gateway-proxy",
        "/mcp",
        "/api/2.0/mlflow/gateway/x",
    ]:
        assert out[path]["status"] == 404, path
        assert out[path]["body"] == "", path  # Caddy's own empty 404, never the upstream's
