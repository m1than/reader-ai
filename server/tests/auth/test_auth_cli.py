"""SRV-AUTH `reader-server`: roles, healthcheck, bootstrap-link, startup validation, and the password canary.

Runs the entrypoint as a subprocess (`python -m reader.serve`) with the test hooks READER_DB_*,
READER_SECRETS_ROOT and READER_RUN_DIR; the defaults are the container paths (postgres:5432, /run/secrets,
/tmp).

@trace FR-AUTH-1 FR-AUTH-2 FR-AUTH-4 FR-AUTH-16 INV-AUTH-5
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import re
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


@pytest.mark.parametrize(
    "args", [["--role", "api", "--lane", "judge"], ["--role", "worker", "--lane", "fast"]]
)
def test_lane_only_main_or_judge_and_only_for_the_worker(tmp_path: Path, args: list[str]) -> None:
    """FR-AUTH-2 (paired SRV-EVENTS FR-EVENTS-9): `--lane main|judge` only with `--role worker`."""
    r = _run(_closed_db_env(tmp_path), *args)
    assert r.returncode == 2


def test_lane_defaults_to_main_and_reaches_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    from reader.serve import main

    seen: list[str] = []
    monkeypatch.setattr(main, "_run_worker", lambda env, lane="main": seen.append(lane))
    monkeypatch.setattr(main.Env, "read", classmethod(lambda cls, environ: None))
    assert main.run(["--role", "worker"], {}) == 0
    assert main.run(["--role", "worker", "--lane", "judge"], {}) == 0
    assert seen == ["main", "judge"]


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


def _worker_output(env: dict[str, str], seconds: float = 3.5) -> tuple[int, str]:
    worker = subprocess.Popen(
        server_cmd("--role", "worker"), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        time.sleep(seconds)
        worker.send_signal(signal.SIGTERM)
        out, err = worker.communicate(timeout=20)
        return worker.returncode, out + err
    finally:
        if worker.poll() is None:
            worker.kill()


def test_worker_password_canary_connection_refused(tmp_path: Path) -> None:
    code, output = _worker_output(_closed_db_env(tmp_path))
    assert code == 0
    assert output.splitlines() == ["reader-server: database_unreachable"]  # several polls, one line per 30 s
    assert CANARY not in output


def test_worker_password_canary_authentication_failed(pg: Pg, tmp_path: Path) -> None:
    code, output = _worker_output(_env(pg, tmp_path, password=CANARY))
    assert code == 0
    assert output.splitlines() == ["reader-server: database_unreachable"]
    assert CANARY not in output


def test_empty_password_file_is_secret_missing(pg: Pg, tmp_path: Path) -> None:
    env = _env(pg, tmp_path, password="")
    for args in (["--role", "worker", "--healthcheck"], ["bootstrap-link"]):
        r = _run(env, *args)
        assert r.returncode == 1
        assert _one_line(r) == "reader-server: secret_missing"


@pytest.mark.parametrize("bind", ["localhost", "127.0.0.1:", "127.0.0.1:http", ":8000", "127.0.0.1:70000"])
def test_invalid_bind_is_one_line(tmp_path: Path, bind: str) -> None:
    r = _run(_closed_db_env(tmp_path), "--role", "api", "--bind", bind)
    assert r.returncode == 1
    assert _one_line(r) == "reader-server: bind_invalid"


@pytest.mark.parametrize("args", [["--role", "worker", "--healthcheck"], ["bootstrap-link"]])
def test_non_numeric_db_port_is_one_line(tmp_path: Path, args: list[str]) -> None:
    env = {**_closed_db_env(tmp_path), "READER_DB_PORT": "five"}
    r = _run(env, *args)
    assert r.returncode == 1
    assert _one_line(r) == "reader-server: config_env_invalid"


def test_unwritable_run_dir_is_one_line(pg: Pg, tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    env = {**_env(pg, tmp_path), "READER_RUN_DIR": str(blocker / "run")}
    r = _run(env, "--role", "api", "--bind", f"127.0.0.1:{_free_port()}")
    assert r.returncode == 1
    assert _one_line(r) == "reader-server: run_dir_unwritable"


def test_database_failure_while_api_runs(pg_container: tuple[Pg, Any], pg: Pg, tmp_path: Path) -> None:
    """The api's own role loses its login mid-run: requests answer 503, and no password or traceback leaks."""
    _, container = pg_container
    role = "r_" + pg.database
    for sql in [
        f"CREATE ROLE {role} LOGIN PASSWORD '{CANARY}'",
        f"ALTER DATABASE {pg.database} OWNER TO {role}",
    ]:
        code, out = container.exec(["psql", "-U", "postgres", "-c", sql])
        assert code == 0, out
    # SRV-SEARCH's migration needs the `vector` extension, which only a superuser creates (FR-SEARCH-4 note).
    code, out = container.exec(["psql", "-U", "postgres", "-d", pg.database, "-c", "CREATE EXTENSION vector"])
    assert code == 0, out
    env = _env(Pg(pg.host, pg.port, role, CANARY, pg.database), tmp_path)
    server = ApiServer(env, _free_port())
    try:
        server.start()
        for sql in [
            f"ALTER ROLE {role} NOLOGIN",
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '{role}'",
        ]:
            code, out = container.exec(["psql", "-U", "postgres", "-c", sql])
            assert code == 0, out
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.port}/api/me", headers={"Cookie": "__Host-reader_session=x"}
        )
        with pytest.raises(urllib.error.HTTPError) as failed:
            urllib.request.urlopen(req, timeout=10)  # noqa: S310
        assert failed.value.code == 503
        assert json.loads(failed.value.read()) == {"error": "unavailable"}
    finally:
        output = server.stop()
    assert CANARY not in output
    assert "Traceback" not in output
    assert "database_unreachable /api/me" in output


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


@pytest.fixture
def restore_logging() -> Iterator[None]:
    root, reader = logging.getLogger(), logging.getLogger("reader")
    saved = (list(root.handlers), root.level, list(reader.handlers), reader.level, reader.propagate)
    yield
    root.handlers[:], root.level = saved[0], saved[1]
    reader.handlers[:], reader.level, reader.propagate = saved[2], saved[3], saved[4]


@pytest.mark.usefixtures("restore_logging")
def test_serving_logs_show_third_party_errors_without_stack_or_args(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The logging the api and worker install once serving (FR-AUTH-2)."""
    from reader.serve.main import serving_logs

    serving_logs()
    secret = "CANARY-arg-" + "y" * 12

    def fail() -> None:
        raise ValueError("CANARY-exc-detail")

    try:
        fail()
    except ValueError:
        logging.getLogger("sqlalchemy.pool.impl").error("Exception during reset %s", secret, exc_info=True)
        logging.getLogger("asyncio").error(ValueError(secret))
        logging.getLogger("reader.auth").warning("auth failure %s %s", "/api/x", "malformed", exc_info=True)
        logging.getLogger("uvicorn.error").error("Exception in ASGI application\n", exc_info=True)
    logging.getLogger("uvicorn.error").info("Started server process")  # below WARNING: dropped
    _unretrieved_task_exception("CANARY-task-" + "w" * 12)
    err = capsys.readouterr().err
    frame = re.escape(" [ValueError at test_auth_cli.py:") + r"\d+ fail\]"
    patterns = [
        re.escape("ERROR sqlalchemy.pool.impl: Exception during reset %s") + frame,
        re.escape("ERROR asyncio: <ValueError>"),
        re.escape("WARNING reader.auth: auth failure /api/x malformed") + frame,
        re.escape("ERROR uvicorn.error: Exception in ASGI application") + frame,
        re.escape("ERROR asyncio: Task exception was never retrieved")
        + re.escape(" [ValueError at test_auth_cli.py:")
        + r"\d+ boom\]",
    ]
    lines = err.splitlines()
    assert len(lines) == len(patterns), lines
    for line, pattern in zip(lines, patterns, strict=True):
        assert re.fullmatch(pattern, line), line
    assert "Traceback" not in err
    for canary in (secret, "CANARY-exc-detail", "CANARY-task-"):
        assert canary not in err


def _unretrieved_task_exception(detail: str) -> None:
    """asyncio's own record: the task repr, with the exception text, sits in record.msg after a newline."""

    async def boom() -> None:
        raise ValueError(detail)

    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(boom())
        loop.run_until_complete(asyncio.sleep(0.01))
        assert task.done()
        del task
        gc.collect()
    finally:
        loop.close()
