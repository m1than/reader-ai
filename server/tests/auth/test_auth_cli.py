"""SRV-AUTH `reader-server`: roles, healthcheck, bootstrap-link, startup validation, and the password canary.

Runs the entrypoint as a subprocess (`python -m reader.serve`) with the test hooks READER_DB_*,
READER_SECRETS_ROOT and READER_RUN_DIR; the defaults are the container paths (postgres:5432, /run/secrets,
/tmp).

@trace FR-AUTH-1 FR-AUTH-2 FR-AUTH-4 FR-AUTH-16 INV-AUTH-5
"""

from __future__ import annotations

import json
import signal
import socket
import subprocess
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from auth_helpers import cli_env, config_text, server_cmd, unb64url, write_config
from conftest import Pg

CANARY = "CANARY-pw-" + "z" * 16


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _paths(tmp_path: Path, password: str, **config: Any) -> tuple[Path, Path, Path]:
    cfg = write_config(tmp_path / "cfg", config_text(**config))
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "db-password").write_text(password, encoding="utf-8")
    run = tmp_path / "run"
    run.mkdir()
    return cfg, secrets, run


def _run(env: dict[str, str], *args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        server_cmd(*args), env=env, capture_output=True, text=True, timeout=timeout, check=False
    )


def _env(pg: Pg, tmp_path: Path, password: str | None = None, **config: Any) -> dict[str, str]:
    cfg, secrets, run = _paths(tmp_path, pg.password if password is None else password, **config)
    return cli_env(pg, cfg, secrets, run)


def _closed_db_env(tmp_path: Path, password: str = CANARY, **config: Any) -> dict[str, str]:
    cfg, secrets, run = _paths(tmp_path, password, **config)
    return cli_env(Pg("127.0.0.1", _free_port(), "postgres", "", "postgres"), cfg, secrets, run)


def test_mcp_role_exits_2(tmp_path: Path) -> None:
    r = _run(_closed_db_env(tmp_path), "--role", "mcp")
    assert r.returncode == 2
    assert "mcp role arrives with SRV-MCP" in r.stdout + r.stderr


@pytest.mark.parametrize("role", ["api", "worker"])
def test_config_error_exits_1_before_connecting(tmp_path: Path, role: str) -> None:
    env = _closed_db_env(tmp_path, users=(("a", "learner"),))
    args = ["--role", role] + (["--bind", "127.0.0.1:1"] if role == "api" else [])
    r = _run(env, *args)
    assert r.returncode == 1
    out = r.stdout + r.stderr
    assert "owner_count" in out
    assert "connect" not in out.lower() and "Traceback" not in out


def _one_line(r: subprocess.CompletedProcess[str]) -> str:
    lines = (r.stdout + r.stderr).strip().splitlines()
    assert len(lines) == 1, lines
    return lines[0]


def test_healthcheck_without_secret_file(tmp_path: Path) -> None:
    env = _closed_db_env(tmp_path)
    (Path(env["READER_SECRETS_ROOT"]) / "db-password").unlink()
    r = _run(env, "--role", "worker", "--healthcheck")
    assert r.returncode == 1
    assert _one_line(r) == "reader-server: secret_missing"


@pytest.mark.parametrize(
    "args",
    [["--role", "worker", "--healthcheck"], ["--role", "api", "--bind", "127.0.0.1:1"], ["bootstrap-link"]],
)
def test_password_canary_connection_refused(tmp_path: Path, args: list[str]) -> None:
    r = _run(_closed_db_env(tmp_path), *args)
    assert r.returncode == 1
    assert _one_line(r) == "reader-server: database_unreachable"
    assert CANARY not in r.stdout + r.stderr


@pytest.mark.parametrize(
    "args",
    [["--role", "worker", "--healthcheck"], ["--role", "api", "--bind", "127.0.0.1:1"], ["bootstrap-link"]],
)
def test_password_canary_authentication_failed(pg: Pg, tmp_path: Path, args: list[str]) -> None:
    r = _run(_env(pg, tmp_path, password=CANARY), *args)
    assert r.returncode == 1
    assert _one_line(r) == "reader-server: database_unreachable"
    assert CANARY not in r.stdout + r.stderr


def test_worker_healthcheck_tracks_schema(pg: Pg, tmp_path: Path) -> None:
    env = _env(pg, tmp_path)
    before = _run(env, "--role", "worker", "--healthcheck")
    assert before.returncode == 1
    assert _one_line(before) == "reader-server: schema_not_at_heads"


def test_worker_waits_without_migrating_and_stops_on_sigterm(pg: Pg, tmp_path: Path) -> None:
    env = _env(pg, tmp_path)
    worker = subprocess.Popen(
        server_cmd("--role", "worker"), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        time.sleep(3)
        assert worker.poll() is None
        assert _run(env, "--role", "worker", "--healthcheck").returncode == 1  # still not migrated
        worker.send_signal(signal.SIGTERM)
        assert worker.wait(timeout=20) == 0
    finally:
        if worker.poll() is None:
            worker.kill()


@pytest.fixture
def api_server(pg: Pg, tmp_path: Path) -> Iterator[ApiServer]:
    env = _env(pg, tmp_path)
    server = ApiServer(env, _free_port())
    yield server
    server.stop()


class ApiServer:
    """`reader-server --role api` as a subprocess, started from the test body."""

    def __init__(self, env: dict[str, str], port: int) -> None:
        self.env, self.port = env, port
        self.proc: subprocess.Popen[str] | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            server_cmd("--role", "api", "--bind", f"127.0.0.1:{self.port}"),
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 60
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/healthz", timeout=1) as r:
                    if r.status == 200:
                        return
            except OSError:
                pass
            assert self.proc.poll() is None, self.proc.communicate()
            assert time.monotonic() < deadline, "api did not become healthy"
            time.sleep(0.3)

    def stop(self) -> str:
        if self.proc is None:
            return ""
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
        try:
            out, err = self.proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            out, err = self.proc.communicate()
        return out + err


def test_api_serves_migrates_and_healthchecks(api_server: ApiServer) -> None:
    api_server.start()
    env = api_server.env
    assert _run(env, "--role", "api", "--healthcheck").returncode == 0
    assert _run(env, "--role", "worker", "--healthcheck").returncode == 0
    output = api_server.stop()
    assert "Traceback" not in output
    stopped = _run(env, "--role", "api", "--healthcheck")
    assert stopped.returncode == 1
    assert _one_line(stopped) == "reader-server: api_unhealthy"


def test_bootstrap_link_and_http_ceremony_keep_secrets_out_of_output(api_server: ApiServer) -> None:
    from softauthn import SoftAuthenticator

    api_server.start()
    env, port = api_server.env, api_server.port
    link = _run(env, "bootstrap-link")
    assert link.returncode == 0, link.stderr
    assert link.stderr == ""
    url = link.stdout.strip()
    assert link.stdout == url + "\n"
    assert url.startswith("https://localhost/api/auth/bootstrap#")
    token = url.split("#", 1)[1]
    assert len(unb64url(token)) == 32

    base = f"http://127.0.0.1:{port}"
    headers = {"Origin": "https://localhost", "Content-Type": "application/json"}

    def post(path: str, body: Any, cookie: str = "") -> tuple[int, Any, list[str]]:
        req = urllib.request.Request(  # noqa: S310 (fixed http://127.0.0.1 base)
            base + path,
            data=json.dumps(body).encode(),
            method="POST",
            headers={**headers, **({"Cookie": cookie} if cookie else {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
                return r.status, json.loads(r.read() or b"null"), r.headers.get_all("Set-Cookie") or []
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null"), e.headers.get_all("Set-Cookie") or []

    status, opts, cookies = post("/api/auth/bootstrap/options", {"token": token})
    assert status == 200
    ceremony = cookies[0].split(";", 1)[0]
    auth = SoftAuthenticator.random()
    status, _, cookies = post(
        "/api/auth/bootstrap/verify", auth.register(challenge=unb64url(opts["challenge"])), ceremony
    )
    assert status == 200
    session = next(c for c in cookies if c.startswith("__Host-reader_session=")).split(";", 1)[0]

    again = _run(env, "bootstrap-link")
    assert again.returncode == 1
    assert "user already has a passkey" in again.stdout + again.stderr
    assert token not in again.stdout + again.stderr
    learner = _run(env, "bootstrap-link", "--handle", "live-check")
    assert learner.returncode == 0 and learner.stdout.startswith("https://localhost/api/auth/bootstrap#")
    unknown = _run(env, "bootstrap-link", "--handle", "nobody")
    assert unknown.returncode == 1 and unknown.stdout == ""

    output = api_server.stop()
    password = (Path(env["READER_SECRETS_ROOT"]) / "db-password").read_text(encoding="utf-8")
    for secret in [token, ceremony.split("=", 1)[1], session.split("=", 1)[1], password]:
        assert secret not in output
