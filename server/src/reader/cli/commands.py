"""`reader` commands for the stack: up, down, status, watcher, secrets set, bootstrap (FR-CLI-1 to FR-CLI-14,
FR-CLI-21). Every external effect goes through the Ctx, so tests swap each one for a fake."""

import functools
import json
import re
import secrets
import sys
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Concatenate, Final, TextIO

import keyring.errors

from reader.cli import ingest_watch
from reader.cli.errors import CliError
from reader.cli.log import LOGGER, log_event
from reader.cli.resolve import (
    SYSTEM_SERVICE,
    USER_SERVICE,
    Keyring,
    check_value,
    host_resolvers,
    resolve_values,
)
from reader.cli.runner import CommandFailed, CommandTimeout, Result, Runner, Spawner, WatcherProcess
from reader.cli.stack import (
    Plan,
    Rendered,
    build_override,
    check_config_path,
    check_handle,
    load_compose_source,
    make_plan,
)
from reader.cli.watcher import encode_bundle, lock_holder, stop_watcher
from reader.config import (
    ConfigError,
    ReaderConfig,
    Secret,
    SecretRef,
    SecretResolutionError,
    effective,
    load_config,
)

READY_TIMEOUT: Final = 10  # s for the watcher's readiness byte (FR-CLI-8): two local daemon calls
AGENT_PROXY_IMAGE: Final = "reader-agent-proxy:local"
HEALTH_TIMEOUT: Final = 180  # s from `up -d` (FR-CLI-10)
HEALTH_POLL: Final = 2  # s, the services' own healthcheck interval (compose.yaml x-health)
LINK_TIMEOUT: Final = 15  # s, FR-CLI-21
OPEN_TIMEOUT: Final = 10  # s, FR-CLI-21
SECRET_NAME: Final = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
TOKEN_CHARS: Final = re.compile(r"[A-Za-z0-9_-]+")
LINK_PASSKEY: Final = "the user already has a passkey, or the handle is unknown"
LINK_FAILED: Final = "could not create the sign-up link (see `reader status`)"
OPEN_FAILED: Final = "could not open the sign-up link in the browser"


@dataclass
class Ctx:
    runner: Runner
    keyring: Keyring
    env: Mapping[str, str]
    run_dir: Path
    log_dir: Path
    compose_file: Path
    spawner: Spawner
    out: TextIO
    err: TextIO
    stdin: TextIO
    is_tty: bool
    getpass: Callable[[str], str]
    ask: Callable[[str], bool]
    sleep: Callable[[float], None]
    monotonic: Callable[[], float]
    kill: Callable[[int, int], None]
    # FR-CLI-23: starts `reader-ingest --watch` detached; None starts no ingest watcher
    ingest_spawn: Callable[[list[str], Mapping[str, str], Path], int] | None = None


def guarded[**P](command: Callable[Concatenate[Ctx, P], int]) -> Callable[Concatenate[Ctx, P], int]:
    """Exit codes: 2 for a configuration error (SRV-CONFIG's text), 1 for any other refusal."""

    @functools.wraps(command)
    def run(ctx: Ctx, /, *args: P.args, **kwargs: P.kwargs) -> int:
        try:
            return command(ctx, *args, **kwargs)
        except ConfigError as error:
            print(str(error), file=ctx.err)
            return 2
        except SecretResolutionError as error:
            print(f"error: cannot resolve {error.ref}: {error.code}", file=ctx.err)
            return 1
        except CliError as error:
            print(f"error: {error}", file=ctx.err)
            return 1

    return run


# The project ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Project:
    config: ReaderConfig
    override: bytes
    model: Rendered
    profiles: tuple[str, ...] = ()


ACTIVE_PROFILES: tuple[str, ...] = ("kernels",)  # FR-CLI-5: the default profile plus these, always
# FR-CLI-5, FR-CLI-22: chosen by `reader up`, recorded in run/profiles
OPTIONAL_PROFILES: tuple[str, ...] = ("agents", "observability")
TRACES_URL: Final = "https://localhost:8443/"  # FR-CLI-22: the dashboard, published only with `observability`


def compose_argv(ctx: Ctx, *args: str, extra: Sequence[str] = ()) -> list[str]:
    """`docker compose --profile … -f deploy/compose.yaml -f - …` (FR-CLI-5; override on stdin, INV-CLI-6)."""
    names = sorted({*ACTIVE_PROFILES, *extra})
    profiles = [flag for p in names for flag in ("--profile", p)]
    return ["docker", "compose", *profiles, "-f", str(ctx.compose_file), "-f", "-", *args]


def compose(ctx: Ctx, override: bytes, *args: str, extra: Sequence[str] = ()) -> Result:
    return ctx.runner.run(compose_argv(ctx, *args, extra=extra), env=ctx.env, stdin=override)


def recorded_profiles(ctx: Ctx) -> tuple[str, ...]:
    """The optional profiles the last `reader up` activated (FR-CLI-5), or none."""
    try:
        names = (ctx.run_dir / "profiles").read_text(encoding="utf-8").split()
    except OSError:
        return ()
    return tuple(sorted(n for n in names if n in OPTIONAL_PROFILES))


def choose_profiles(ctx: Ctx, config: ReaderConfig) -> tuple[str, ...]:
    """FR-CLI-5: `agents` when a provider is configured and the agent-proxy image exists; otherwise it stays
    off with one line on stderr and `up` continues. FR-CLI-22: `observability` when the config enables it."""
    chosen: list[str] = []
    if any(u.providers for u in config.users):
        found = docker(ctx, "docker", "image", "inspect", AGENT_PROXY_IMAGE, timeout=30)
        if found is not None and found.code == 0:
            chosen.append("agents")
        else:
            print(f"note: profile agents stays off: image {AGENT_PROXY_IMAGE} does not exist", file=ctx.err)
    if config.observability.enabled:
        chosen.append("observability")
    return tuple(chosen)


def profile_services(ctx: Ctx, profile: str) -> list[str]:
    """The services of `profile` in compose.yaml's source (FR-CLI-5 withdrawal)."""
    services = load_compose_source(ctx.compose_file).get("services") or {}
    return sorted(
        name
        for name, body in services.items()
        if isinstance(body, dict) and profile in (body.get("profiles") or [])
    )


def load(
    ctx: Ctx, config_path: Path, profiles: Sequence[str] | None = None, config: ReaderConfig | None = None
) -> Project:
    config = config if config is not None else load_config(config_path)
    active = tuple(sorted(profiles)) if profiles is not None else recorded_profiles(ctx)
    path = check_config_path(config_path)
    override = build_override(
        load_compose_source(ctx.compose_file), [u.handle for u in config.users], path,
        observability="observability" in active,
    )
    data = json.dumps(override).encode()
    rendered = _checked(
        ctx, lambda: compose(ctx, data, "config", "--format", "json", extra=active), "compose_config"
    )
    parsed: Any = None
    try:
        parsed = json.loads(rendered.out)
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        raise CliError("bad_model", "docker compose config did not return a JSON object")
    return Project(config, data, Rendered.parse(parsed), active)


def _checked(ctx: Ctx, call: Callable[[], Result], code: str) -> Result:
    result: Result | None = None
    try:
        result = call()
    except CommandFailed, CommandTimeout:
        result = None
    if result is None or result.code != 0:
        raise CliError(code, "a docker command failed (is Docker running?)")
    return result


def docker(ctx: Ctx, *argv: str, stdin: bytes | None = None, timeout: float | None = None) -> Result | None:
    try:
        return ctx.runner.run(list(argv), env=ctx.env, stdin=stdin, timeout=timeout)
    except CommandFailed, CommandTimeout:
        return None


def ensure_run_dir(ctx: Ctx) -> None:
    ctx.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    ctx.run_dir.chmod(0o700)


def recorded_project(ctx: Ctx) -> str | None:
    try:
        name = (ctx.run_dir / "project").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return name or None


def project_name(ctx: Ctx, config_path: Path) -> tuple[str | None, set[str] | None]:
    """FR-CLI-3's name and default-profile services when the config is valid, else the one `up` recorded."""
    try:
        model = load(ctx, config_path).model
    except ConfigError, CliError, OSError:
        return recorded_project(ctx), None
    return model.name, set(model.services)


def watcher_running(ctx: Ctx) -> bool:
    return lock_holder(ctx.run_dir / "watcher.lock") is not None


def _ingest_data_dir(config_path: Path) -> Path | None:
    """The ingest watcher's data_dir from reader.yaml; None when the configuration cannot be loaded."""
    try:
        return ingest_watch.data_dir_of(load_config(config_path), config_path)
    except ConfigError, OSError:
        return None


# Health and status (FR-CLI-10, FR-CLI-13) -----------------------------------------------------------------


def ps(ctx: Ctx, project: str) -> list[dict[str, Any]] | None:
    result = docker(ctx, "docker", "compose", "-p", project, "ps", "--format", "json")
    if result is None or result.code != 0:
        return None
    text = result.out.decode("utf-8", "replace").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        rows = parsed if isinstance(parsed, list) else [parsed]
    except ValueError:
        try:
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        except ValueError:
            return None
    return [row for row in rows if isinstance(row, dict)]


def status_code(services: Sequence[Mapping[str, Any]], expected: Collection[str], watcher: bool) -> int:
    """Bit 1: a default-profile service is not running or not healthy. Bit 2: the watcher is stopped."""
    state = {str(s.get("Service")): s for s in services}
    unhealthy = not expected or any(
        name not in state
        or state[name].get("State") != "running"
        or state[name].get("Health") not in ("healthy", "", None)
        for name in expected
    )
    return (1 if unhealthy else 0) | (0 if watcher else 2)


def wait_healthy(ctx: Ctx, model: Rendered) -> bool:
    deadline = ctx.monotonic() + HEALTH_TIMEOUT
    while True:
        services = ps(ctx, model.name) or []
        if status_code(services, set(model.services), True) == 0:
            return True
        if ctx.monotonic() >= deadline:
            return False
        ctx.sleep(HEALTH_POLL)


@guarded
def cmd_status(ctx: Ctx, config_path: Path, as_json: bool = False) -> int:
    project, expected = project_name(ctx, config_path)
    if project is None:
        print("error: no project (run `reader up` first)", file=ctx.err)
        return 4
    services = ps(ctx, project)
    if services is None:
        print("error: docker is unreachable", file=ctx.err)
        return 4
    watcher = watcher_running(ctx)
    expected = expected if expected is not None else {str(s.get("Service")) for s in services}
    code = status_code(services, expected, watcher)
    rows = [
        {"name": str(s.get("Service")), "state": str(s.get("State")), "health": s.get("Health") or None}
        for s in services
    ]
    data_dir = _ingest_data_dir(config_path)  # FR-CLI-23
    # Without a loadable reader.yaml the watcher's lock cannot be found: `unknown`, never `stopped`.
    problem = None if data_dir else ("config_missing" if not config_path.is_file() else "config_invalid")
    ingest = ingest_watch.state(data_dir, ingest_watch.checkout_sha()) if data_dir else "unknown"
    converting = ingest_watch.converter(data_dir) if data_dir else "unknown"
    overrides = _status_overrides(ctx, project, config_path)
    if as_json:
        body: dict[str, Any] = {
            "project": project,
            "watcher": "running" if watcher else "stopped",
            "ingest_watcher": ingest,
            "converter": converting,
            "services": rows,
            "overrides": overrides,
        }
        if problem:
            body["ingest_config"] = {"problem": problem, "path": str(config_path)}
        print(json.dumps(body), file=ctx.out)
    else:
        print(
            f"project {project}; watcher {'running' if watcher else 'stopped'}; ingest watcher {ingest};"
            f" converter {converting}",
            file=ctx.out,
        )
        if problem:
            print(
                f"  ingest watcher unknown: {problem} ({config_path}); "
                "pass --config <the reader.yaml of `reader up`>",
                file=ctx.out,
            )
        for row in rows:
            print(f"  {row['name']}: {row['state']} {row['health'] or ''}".rstrip(), file=ctx.out)
        for line in overrides:
            print(f"  {line}", file=ctx.out)
    return code


# Settings overrides (SRV-CONFIG FR-CONFIG-26, paired) ---------------------------------------------------

OVERRIDES_SQL: Final = "SELECT key, value FROM settings_overrides ORDER BY key"
OVERRIDES_TIMEOUT: Final = 30  # s, one local psql query
OVERRIDES_UNAVAILABLE: Final = "overrides: unavailable"


def override_lines(ctx: Ctx, project: str, config: ReaderConfig) -> list[str]:
    """One read of the overlay through the postgres container (no database driver here), evaluated
    against the loaded reader.yaml with SRV-CONFIG's own rules; a failed read is one `unavailable` line,
    never an error."""
    result = docker(
        ctx, "docker", "compose", "-p", project, "exec", "-T", "postgres",
        "psql", "-U", "postgres", "-d", "postgres", "-At", "-c", OVERRIDES_SQL,
        timeout=OVERRIDES_TIMEOUT,
    )
    if result is None or result.code != 0:
        return [OVERRIDES_UNAVAILABLE]
    stored: dict[str, Any] = {}
    for line in result.out.decode("utf-8", "replace").splitlines():
        key, sep, raw = line.partition("|")
        if not line.strip():
            continue
        try:
            stored[key] = json.loads(raw) if sep else None
        except ValueError:
            return [OVERRIDES_UNAVAILABLE]
    return effective.status_lines(effective.file_settings(config), stored)


def _status_overrides(ctx: Ctx, project: str, config_path: Path) -> list[str]:
    try:
        config = load_config(config_path)
    except ConfigError, OSError:
        return []  # without the reader.yaml of `reader up` the overrides cannot be evaluated
    return override_lines(ctx, project, config)


# The DB password (FR-CLI-14) ------------------------------------------------------------------------------


def _volume_state(ctx: Ctx, volume: str) -> str:
    result = docker(ctx, "docker", "volume", "inspect", volume)
    if result is None:
        return "error"
    if result.code == 0:
        return "present"
    return "absent" if b"no such volume" in (result.out + result.err).lower() else "error"


def ensure_db_password(ctx: Ctx, volume: str) -> Secret:
    """Generated only on a definite "not found" from both the Keychain item and the pgdata volume."""
    ref = SecretRef(f"secret://keychain/{SYSTEM_SERVICE}/postgres")
    value: str | None = None
    failed = False
    try:
        value = ctx.keyring.get_password(SYSTEM_SERVICE, "postgres")
    except keyring.errors.KeyringError:
        failed = True
    if failed:
        raise CliError("keychain_error", f"cannot read the Keychain item {SYSTEM_SERVICE}/postgres")
    if value is not None:
        return check_value(ref, value)
    state = _volume_state(ctx, volume)
    if state == "present":
        raise CliError(
            "db_password_missing",
            f"volume {volume} exists but the Keychain item "
            f"{SYSTEM_SERVICE}/postgres does not; the database password cannot be regenerated",
        )
    if state != "absent":
        raise CliError("docker_error", f"cannot inspect volume {volume}")
    password = secrets.token_urlsafe(32)
    stored = True
    try:
        ctx.keyring.set_password(SYSTEM_SERVICE, "postgres", password)
    except keyring.errors.KeyringError:
        stored = False
    if not stored:
        raise CliError("keychain_error", f"cannot store the Keychain item {SYSTEM_SERVICE}/postgres")
    log_event(LOGGER, "db_password_generated", item=f"{SYSTEM_SERVICE}/postgres")
    return Secret(password)


# up and down (FR-CLI-7, FR-CLI-8, FR-CLI-10) --------------------------------------------------------------


def watcher_argv(ctx: Ctx, project: str, since: str, plan: Plan) -> list[str]:
    return [
        sys.executable,
        "-m",
        "reader.cli.watcher",
        "--project",
        project,
        "--since",
        since,
        "--plan",
        json.dumps([asdict(d) for d in plan.deliveries]),
        "--run-dir",
        str(ctx.run_dir),
        "--log-dir",
        str(ctx.log_dir),
    ]


def offer_trust(ctx: Ctx, project: str) -> None:
    """FR-CLI-16: only an interactive `reader up` offers trust; agent-run (non-TTY) runs skip it."""
    if not ctx.is_tty:
        return
    if ctx.ask("Trust the local Caddy root for TLS in your login keychain? [y/N] "):
        from reader.cli.maintenance import trust_root

        trust_root(ctx, project)


@guarded
def cmd_up(ctx: Ctx, config_path: Path) -> int:
    config = load_config(config_path)  # a configuration error exits 2 before any docker call
    check_config_path(config_path)
    previous = recorded_profiles(ctx)
    project = load(ctx, config_path, choose_profiles(ctx, config), config)
    model = project.model
    ensure_run_dir(ctx)
    (ctx.run_dir / "project").write_text(model.name + "\n", encoding="utf-8")
    _checked(ctx, lambda: compose(ctx, project.override, "build", extra=project.profiles), "build_failed")
    since = (
        _checked(
            ctx,
            lambda: ctx.runner.run(["docker", "info", "--format", "{{.SystemTime}}"], env=ctx.env),
            "docker_unreachable",
        )
        .out.decode()
        .strip()
    )
    db_password = ensure_db_password(ctx, model.pgdata_volume)
    plan = make_plan(model, project.config)
    values = resolve_values(plan, host_resolvers(ctx.keyring, ctx.runner, ctx.env), db_password)
    stop_watcher(ctx)  # after resolution succeeded, so a failed `up` leaves the running watcher in place
    process: WatcherProcess | None = None
    try:
        process = ctx.spawner.spawn(
            watcher_argv(ctx, model.name, since, plan), env=ctx.env, bundle=encode_bundle(values)
        )
    except CommandFailed, OSError:
        process = None
    del values
    if process is None:
        raise CliError("spawn_failed", "the watcher could not be started; no container was started")
    if not process.wait_ready(READY_TIMEOUT):
        process.terminate()
        raise CliError("watcher_not_ready", "the watcher did not signal readiness; no container was started")
    withdrawn = [s for p in previous if p not in project.profiles for s in profile_services(ctx, p)]
    if withdrawn:  # FR-CLI-5: the withdrawn profile's provider tokens leave tmpfs with its containers
        removed = docker(ctx, "docker", "compose", "-p", model.name, "rm", "-sf", *withdrawn)
        if removed is None or removed.code != 0:
            process.terminate()
            raise CliError(
                "withdraw_failed", f"could not remove {', '.join(withdrawn)}; the watcher was stopped"
            )
    # Recorded only now, after resolution and readiness: a failed `up` leaves the previous record.
    (ctx.run_dir / "profiles").write_text("".join(p + "\n" for p in project.profiles), encoding="utf-8")
    started = docker(ctx, *compose_argv(ctx, "up", "-d", extra=project.profiles), stdin=project.override)
    if started is None or started.code != 0:
        process.terminate()
        raise CliError("up_failed", "docker compose up failed; the watcher was stopped")
    if not wait_healthy(ctx, model):
        print(
            f"error: not every service is healthy after {HEALTH_TIMEOUT} s (see `reader status`)",
            file=ctx.err,
        )
        return 1
    ingest_watch.ensure(ctx, config_path, config)  # FR-CLI-23; a failed spawn is reported, never fatal
    offer_trust(ctx, model.name)
    print(f"reader is up ({model.name}); the watcher keeps delivering secrets", file=ctx.out)
    for line in override_lines(ctx, model.name, config):  # SRV-CONFIG FR-CONFIG-26 (paired)
        print(f"  {line}", file=ctx.out)
    if "observability" in project.profiles:  # FR-CLI-22
        print(f"traces: {TRACES_URL}", file=ctx.out)
    return 0


@guarded
def cmd_down(ctx: Ctx, config_path: Path, untrust: bool = False) -> int:
    project, _ = project_name(ctx, config_path)
    stop_watcher(ctx)
    data_dir = _ingest_data_dir(config_path)
    if data_dir is not None:
        ingest_watch.stop(ctx, data_dir)  # FR-CLI-23: before `docker compose down`
    code = 0
    if project is None:
        print("error: no project (nothing was started by `reader up`)", file=ctx.err)
        code = 1
    else:
        result = docker(ctx, "docker", "compose", "-p", project, "down", "--remove-orphans")
        code = 0 if result is not None and result.code == 0 else 1
    if untrust:
        from reader.cli.maintenance import untrust_all

        code |= untrust_all(ctx)
    return code


def cmd_watcher_status(ctx: Ctx) -> int:
    running = watcher_running(ctx)
    print("running" if running else "stopped", file=ctx.out)
    return 0 if running else 3


def cmd_watcher_stop(ctx: Ctx) -> int:
    print("stopped" if stop_watcher(ctx) else "not running", file=ctx.out)
    return 0


# secrets set (FR-CLI-12) ----------------------------------------------------------------------------------


def cmd_secrets_set(ctx: Ctx, name: str) -> int:
    if not SECRET_NAME.fullmatch(name) or name == "postgres" or name.startswith("system"):
        print(
            "error: NAME must match [a-z0-9][a-z0-9._-]{0,63} and must not be reserved (postgres, system*)",
            file=ctx.err,
        )
        return 2
    value: str | None = None
    if ctx.is_tty:
        value = ctx.getpass(f"value for {name}: ")
    else:
        try:
            raw = ctx.stdin.read()
            value = raw[:-1] if raw.endswith("\n") else raw
        except UnicodeDecodeError:  # the error holds the bytes: dropped here, never chained or printed
            value = None
    ref = SecretRef(f"secret://keychain/{USER_SERVICE}/{name}")
    code: str | None = "not_utf8"
    secret: Secret | None = None
    if value is not None:
        try:
            secret = check_value(ref, value)
        except SecretResolutionError as error:
            code = error.code
    del value
    if secret is None:
        print(f"error: the value is not accepted: {code}", file=ctx.err)
        return 1
    stored = True
    try:
        ctx.keyring.set_password(USER_SERVICE, name, secret.reveal())
    except keyring.errors.KeyringError:
        stored = False
    if not stored:
        print("error: cannot store the value in the Keychain", file=ctx.err)
        return 1
    print(ref, file=ctx.out)
    return 0


# bootstrap (FR-CLI-21) ------------------------------------------------------------------------------------


def parse_link(out: bytes, prefix: str) -> str | None:
    """Exactly one line: the literal prefix, then a non-empty run of [A-Za-z0-9_-]."""
    text: str | None
    try:
        text = out.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is None:
        return None
    if text.endswith("\n"):
        text = text[:-1]
    if "\n" in text or "\r" in text or not text.startswith(prefix):
        return None
    return text if TOKEN_CHARS.fullmatch(text[len(prefix) :]) else None


def _run_quiet(ctx: Ctx, argv: list[str], timeout: float) -> Result | None:
    """A call whose argv may hold the token: any failure becomes None, its exception (and argv) discarded."""
    result: Result | None = None
    try:
        result = ctx.runner.run(argv, env=ctx.env, timeout=timeout)
    except CommandFailed, CommandTimeout, OSError:
        result = None
    return result


@guarded
def cmd_bootstrap(ctx: Ctx, config_path: Path, handle: str | None = None) -> int:
    if handle is not None:
        try:
            check_handle(handle)
        except CliError:
            print("error: the handle must match [a-z0-9][a-z0-9-]* and be at most 50 long", file=ctx.err)
            return 2
    project = load(ctx, config_path)
    services = ps(ctx, project.model.name)
    if services is None or status_code(services, set(project.model.services), watcher_running(ctx)) != 0:
        print(
            "error: the stack is not healthy or the watcher is not running (see `reader status`)",
            file=ctx.err,
        )
        return 1
    argv = [
        "docker",
        "compose",
        "-p",
        project.model.name,
        "exec",
        "-T",
        "api",
        "reader-server",
        "bootstrap-link",
    ]
    if handle is not None:
        argv += ["--handle", handle]
    link = _run_quiet(ctx, argv, LINK_TIMEOUT)
    if link is not None and link.code == 1:
        print(f"error: {LINK_PASSKEY}", file=ctx.err)
        return 1
    prefix = str(project.config.instance.url).rstrip("/") + "/api/auth/bootstrap#"
    url = parse_link(link.out, prefix) if link is not None and link.code == 0 else None
    if url is None:
        print(f"error: {LINK_FAILED}", file=ctx.err)
        return 1
    opened = _run_quiet(ctx, ["open", url], OPEN_TIMEOUT)
    del url
    if opened is None or opened.code != 0:
        print(f"error: {OPEN_FAILED}", file=ctx.err)
        return 1
    print("opened the sign-up link in the browser", file=ctx.out)
    return 0
