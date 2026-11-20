"""`reader-server --role api|worker|mcp [--bind ip:port] [--healthcheck]` and `reader-server bootstrap-link`.

Every failure prints exactly one line, `reader-server: <code>`, and exits 1; no traceback and no value (the
database password, a token) reaches stdout or stderr (INV-AUTH-5). Only `bootstrap-link` writes its link to
stdout. Paths and the database endpoint default to the container's; the READER_* variables of FR-AUTH-1
override them (tests, and OPS-DEPLOY's static `READER_CONFIG`).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
import urllib.request
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from reader.auth.policy import Settings, StartupError, settings_from_config
from reader.config import ConfigError, SecretResolutionError, container_registry, load_config, resolve
from reader.db import DbTarget, at_heads, make_engine, migrate, wait_for_heads

DEFAULT_CONFIG: Final = "/etc/reader/reader.yaml"
DEFAULT_SECRETS_ROOT: Final = "/run/secrets"
DEFAULT_RUN_DIR: Final = "/tmp"  # noqa: S108 (a tmpfs in the container; holds only the api's bound address)
DB_PASSWORD_REF: Final = "secret://file/run/secrets/db-password"  # noqa: S105 (a reference, not a value)
API_ADDR_FILE: Final = "reader-api.addr"
WORKER_ERROR_EVERY_S: Final = 30.0
MAX_PORT: Final = 65535


class Failure(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _line(code: str) -> None:
    sys.stderr.write(f"reader-server: {code}\n")
    sys.stderr.flush()


@dataclass(frozen=True)
class Env:
    config: Path
    secrets_root: Path
    run_dir: Path
    db_host: str
    db_port: int
    db_user: str
    db_name: str

    @classmethod
    def read(cls, environ: Mapping[str, str]) -> Env:
        port = environ.get("READER_DB_PORT", "5432")
        if not port.isdigit() or not 0 < int(port) <= MAX_PORT:
            raise Failure("config_env_invalid")
        return cls(
            config=Path(environ.get("READER_CONFIG", DEFAULT_CONFIG)),
            secrets_root=Path(environ.get("READER_SECRETS_ROOT", DEFAULT_SECRETS_ROOT)),
            run_dir=Path(environ.get("READER_RUN_DIR", DEFAULT_RUN_DIR)),
            db_host=environ.get("READER_DB_HOST", "postgres"),
            db_port=int(port),
            db_user=environ.get("READER_DB_USER", "postgres"),
            db_name=environ.get("READER_DB_NAME", "postgres"),
        )


def _settings(env: Env) -> Settings:
    try:
        config = load_config(env.config)
    except ConfigError as error:
        raise Failure(error.problems[0].code if error.problems else "config_invalid") from None
    try:
        return settings_from_config(config)
    except StartupError as error:
        raise Failure(error.code) from None


def _target(env: Env) -> DbTarget:
    file = env.secrets_root / "db-password"
    try:
        if not file.is_file() or file.stat().st_size == 0:
            raise Failure("secret_missing")
        password = resolve(DB_PASSWORD_REF, container_registry(env.secrets_root))
    except SecretResolutionError, OSError:
        raise Failure("secret_missing") from None
    return DbTarget(env.db_host, env.db_port, env.db_user, env.db_name, password)


def _bind(bind: str) -> tuple[str, int]:
    host, sep, port = bind.rpartition(":")
    if not sep or not host or not port.isdigit() or not 0 < int(port) <= MAX_PORT:
        raise Failure("bind_invalid")
    return host, int(port)


async def _db[T](target: DbTarget, work: Callable[[AsyncEngine], Awaitable[T]]) -> T:
    """Run database work; any failure becomes `database_unreachable` with no detail."""
    engine = make_engine(target)
    try:
        return await work(engine)
    except Failure:
        raise
    except Exception:
        raise Failure("database_unreachable") from None
    finally:
        await engine.dispose()


def _healthcheck(role: str, env: Env) -> None:
    target = _target(env)

    async def check(engine: AsyncEngine) -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        if not await at_heads(engine):
            raise Failure("schema_not_at_heads")

    asyncio.run(_db(target, check))
    if role == "api":
        addr = env.run_dir / API_ADDR_FILE
        try:
            bound = addr.read_text(encoding="utf-8").strip()
            with urllib.request.urlopen(f"http://{bound}/api/healthz", timeout=3) as r:
                if r.status == 200:
                    return
        except OSError, ValueError:
            pass
        raise Failure("api_unhealthy")


def _serve_api(bind: str, env: Env) -> None:
    import uvicorn

    from reader.api.app import create_app
    from reader.auth.store import prepare

    host, port = _bind(bind)
    settings = _settings(env)
    target = _target(env)

    async def run() -> None:
        engine = make_engine(target)
        try:
            try:
                await migrate(engine)
                await prepare(engine, settings)
            except Exception:
                raise Failure("database_unreachable") from None
            try:
                env.run_dir.mkdir(parents=True, exist_ok=True)
                (env.run_dir / API_ADDR_FILE).write_text(bind, encoding="utf-8")
            except OSError:
                raise Failure("run_dir_unwritable") from None
            # Request-time logs (FR-AUTH-18 failure lines, database_unreachable) go to stderr, one line each.
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
            logging.getLogger("reader").addHandler(handler)
            logging.getLogger("reader").setLevel(logging.INFO)
            app = create_app(settings, engine)
            server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, proxy_headers=False))
            await server.serve()
        finally:
            await engine.dispose()

    asyncio.run(run())


def _run_worker(env: Env) -> None:
    _settings(env)
    target = _target(env)
    last_report = [-WORKER_ERROR_EVERY_S]

    def report() -> None:
        now = time.monotonic()
        if now - last_report[0] >= WORKER_ERROR_EVERY_S:
            last_report[0] = now
            _line("database_unreachable")

    async def run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        engine = make_engine(target)
        try:
            await wait_for_heads(engine, stop, on_error=report)
            await stop.wait()  # jobs arrive with V.SRV-JOBS-min
        finally:
            await engine.dispose()

    asyncio.run(run())


def _bootstrap_link(handle: str | None, env: Env) -> None:
    from reader.auth.store import BootstrapRefused, create_bootstrap_token

    settings = _settings(env)
    target = _target(env)
    messages = {"owner_has_credential": "user already has a passkey"}

    async def create(engine: AsyncEngine) -> str:
        try:
            return await create_bootstrap_token(engine, settings, handle)
        except BootstrapRefused as refused:
            raise Failure(messages.get(refused.code, refused.code)) from None

    token = asyncio.run(_db(target, create))
    sys.stdout.write(f"{settings.origin}/api/auth/bootstrap#{token}\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reader-server")
    parser.add_argument("--role", choices=("api", "worker", "mcp"))
    parser.add_argument("--bind", default="0.0.0.0:8000")
    parser.add_argument("--healthcheck", action="store_true")
    return parser


def run(argv: list[str], environ: Mapping[str, str]) -> int:
    if argv[:1] == ["bootstrap-link"]:
        sub = argparse.ArgumentParser(prog="reader-server bootstrap-link")
        sub.add_argument("--handle")
        handle = sub.parse_args(argv[1:]).handle
        _bootstrap_link(handle, Env.read(environ))
        return 0
    args = _parser().parse_args(argv)
    if args.role is None:
        _parser().error("--role is required")
    if args.role == "mcp":
        _line("mcp role arrives with SRV-MCP")
        return 2
    env = Env.read(environ)
    if args.healthcheck:
        _healthcheck(args.role, env)
    elif args.role == "api":
        _serve_api(args.bind, env)
    else:
        _run_worker(env)
    return 0


def main(argv: list[str] | None = None) -> int:
    # No library log record may reach stderr through logging's last-resort handler: the output is one line.
    logging.getLogger().addHandler(logging.NullHandler())
    try:
        return run(sys.argv[1:] if argv is None else argv, os.environ)
    except Failure as failure:
        _line(failure.code)
        return 1
    except KeyboardInterrupt:
        return 130
