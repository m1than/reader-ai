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
from typing import Any, Final

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
        settings = settings_from_config(config)
    except StartupError as error:
        raise Failure(error.code) from None
    bind_policy(env.config)  # every role that reads its settings checks the retrieval policy too
    return settings


def bind_policy(config: Path | None = None) -> None:
    """SRV-SEARCH FR-SEARCH-34 (paired): the retrieval policy loads once per process before serving; a bad
    policy file stops the api and every worker lane with `policy_invalid`."""
    from reader.search.policy import PolicyError, bind

    try:
        bind(config)
    except PolicyError as error:
        raise Failure(error.code) from None


def start_obs(role: str, env: Env) -> Any:
    """FR-AUTH-33: SRV-OBS tracing for this role from the configuration's `observability` section."""
    from reader.obs.setup import setup

    try:
        config = load_config(env.config)
    except ConfigError as error:
        raise Failure(error.problems[0].code if error.problems else "config_invalid") from None
    return setup(role, config)


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
    from reader.auth.store import HandleTakenBySignup, prepare
    from reader.jobs.catalog import load_kinds
    from reader.jobs.queue import Jobs
    from reader.jobs.worker import make_app

    host, port = _bind(bind)
    settings = _settings(env)
    target = _target(env)

    async def run() -> None:
        engine = make_engine(target)
        try:
            try:
                await migrate(engine)
                superseded = await prepare(engine, settings)
            except HandleTakenBySignup as taken:
                # FR-AUTH-24, FR-AUTH-2: the one code naming a value; a handle is no secret (owner note 5).
                raise Failure(f"handle_taken_by_signup {taken.handle}") from None
            except Exception:
                raise Failure("database_unreachable") from None
            try:
                env.run_dir.mkdir(parents=True, exist_ok=True)
                (env.run_dir / API_ADDR_FILE).write_text(bind, encoding="utf-8")
            except OSError:
                raise Failure("run_dir_unwritable") from None
            serving_logs()
            auth_log = logging.getLogger("reader.auth")
            for user_id in superseded:  # best-effort: lost if the api died before this line (owner note 5)
                auth_log.info("auth signup_superseded %s", user_id)
            if settings.signup_default_note:  # FR-AUTH-23
                auth_log.info("auth signup_default_off non_localhost")
            # FR-AUTH-10 note: one SRV-EVENTS Jobs on the declared catalog, its own procrastinate app, for
            # routes that enqueue (SRV-PIPELINE, the tutor's ask).
            queue = make_app(target)
            await queue.open_async()
            obs: Any = None
            try:
                obs = start_obs("api", env)  # FR-AUTH-33 (SRV-OBS FR-OBS-1); a failure still closes the queue
                jobs = Jobs(engine, queue, load_kinds(), (target.password.reveal(),))
                app = create_app(settings, engine, jobs=jobs)
                # log_config=None: uvicorn's records go through serving_logs' root handler (no tracebacks).
                server = uvicorn.Server(
                    uvicorn.Config(app, host=host, port=port, proxy_headers=False, log_config=None)
                )
                await server.serve()
            finally:
                if obs is not None:
                    obs.shutdown()
                await queue.close_async()
        finally:
            await engine.dispose()

    asyncio.run(run())


def _run_worker(env: Env, lane: str = "main") -> None:
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
            if await wait_for_heads(engine, stop, on_error=report):
                serving_logs()
                from reader.jobs.worker import run as run_jobs  # SRV-EVENTS FR-EVENTS-9

                # Every resolved secret this process holds: the worker role holds only the database password.
                held = (target.password.reveal(),)
                obs: Any = None
                try:
                    obs = start_obs("worker", env)  # FR-AUTH-33 (SRV-OBS FR-OBS-1), inside the try
                    code = await run_jobs(engine, target, stop, secret_values=held, lane=lane)
                finally:
                    if obs is not None:
                        obs.shutdown()
                if code != 0:
                    raise Failure("database_unreachable")
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
    # FR-AUTH-2 (paired SRV-EVENTS FR-EVENTS-9, ADR-0039 B7b): the worker's lane; `judge` is worker-judge.
    parser.add_argument("--lane", choices=("main", "judge"))
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
    if args.lane is not None and args.role != "worker":
        _parser().error("--lane is only for --role worker")
    if args.role == "mcp":
        _line("mcp role arrives with SRV-MCP")
        return 2
    env = Env.read(environ)
    if args.healthcheck:
        _healthcheck(args.role, env)
    elif args.role == "api":
        _serve_api(args.bind, env)
    else:
        _run_worker(env, args.lane or "main")
    return 0


_QUIET: Final = logging.NullHandler()


def _first_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[0] if lines else ""


def _exception(record: logging.LogRecord) -> str:
    """` [Type at file:line function]`: the exception type and its innermost frame, no message, no locals."""
    if not record.exc_info or record.exc_info[0] is None:
        return ""
    kind, tb = record.exc_info[0].__name__, record.exc_info[2]
    if tb is None:
        return f" [{kind}]"
    while tb.tb_next is not None:
        tb = tb.tb_next
    code = tb.tb_frame.f_code
    return f" [{kind} at {Path(code.co_filename).name}:{tb.tb_lineno} {code.co_name}]"


class SafeFormatter(logging.Formatter):
    """One line: level, logger name, the first line of the message template, the exception type and frame.

    Third-party records may carry driver detail in their args, in later lines of a pre-formatted message
    (asyncio puts the task repr, with the exception text, there) or in tracebacks; none of those is printed.
    """

    def format(self, record: logging.LogRecord) -> str:
        template = record.msg if isinstance(record.msg, str) else f"<{type(record.msg).__name__}>"
        return f"{record.levelname} {record.name}: {_first_line(template)}{_exception(record)}"


class ReaderFormatter(logging.Formatter):
    """reader.* records: our own messages and args (route, failure reason), one line; never a traceback."""

    def format(self, record: logging.LogRecord) -> str:
        return f"{record.levelname} {record.name}: {_first_line(record.getMessage())}{_exception(record)}"


def serving_logs() -> None:
    """Long-running roles: WARNING and above from any logger reach stderr, one line each, without stack or
    args; reader.* records at INFO with their own args. The one-shot quiet handler is removed."""
    root = logging.getLogger()
    root.removeHandler(_QUIET)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(SafeFormatter())
    handler.setLevel(logging.WARNING)
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    reader = logging.getLogger("reader")
    own = logging.StreamHandler(sys.stderr)
    own.setFormatter(ReaderFormatter())
    from reader.obs.logs import TraceFilter  # FR-AUTH-33 (SRV-OBS FR-OBS-4): ` trace=<32 hex>` inside a span

    own.addFilter(TraceFilter())
    reader.addHandler(own)
    reader.setLevel(logging.INFO)
    reader.propagate = False


def main(argv: list[str] | None = None) -> int:
    # One-shot paths (healthcheck, bootstrap-link, startup before serving): no library record may reach stderr
    # through logging's last-resort handler, so the output stays one line. serving_logs() replaces this.
    logging.getLogger().addHandler(_QUIET)
    try:
        return run(sys.argv[1:] if argv is None else argv, os.environ)
    except Failure as failure:
        _line(failure.code)
        return 1
    except KeyboardInterrupt:
        return 130
