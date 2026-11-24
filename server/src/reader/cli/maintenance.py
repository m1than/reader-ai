"""Trust, doctor, backup and restore (FR-CLI-15 to FR-CLI-18)."""

import hashlib
import json
import os
import plistlib
import re
import ssl
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Final

from reader.cli import obs_store
from reader.cli.commands import (
    JAEGER_URL,
    TRACES_URL,
    Ctx,
    Project,
    _obs_run,
    choose_profiles,
    compose_argv,
    docker,
    ensure_run_dir,
    guarded,
    load,
    ps,
    watcher_running,
)
from reader.cli.errors import CliError
from reader.cli.resolve import SYSTEM_SERVICE, host_resolvers, in_git_work_tree
from reader.cli.stack import Rendered, make_plan, volume_user
from reader.config import SecretResolutionError, load_config, resolve

ROOT_CRT: Final = "/data/caddy/pki/authorities/local/root.crt"
BACKUP_LABEL: Final = "org.reader-ai.backup"
WRITERS: Final = ("api", "worker", "mcp")
RESTORE_SCRIPT: Final = "find /v -mindepth 1 -delete && tar -C /v -xf -"
EMPTY_CHECK: Final = "find /v -mindepth 1 -print -quit"
# Exact row count over every application table. Declared system tables, filled by migrations at first start,
# are excluded (FR-CLI-18): alembic_version. A constant: nothing user-supplied enters the query.
ROWS_SQL: Final = (
    "SELECT coalesce(sum((xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I.%I', "
    "schemaname, tablename), false, true, '')))[1]::text::bigint), 0) FROM pg_tables "
    "WHERE schemaname NOT IN ('pg_catalog', 'information_schema') AND tablename NOT IN ('alembic_version')"
)
DUMP_TIMEOUT: Final = 600  # s, pg_dump / pg_restore: a pilot-sized database; revised from the live drill
ONE_OFF_TIMEOUT: Final = 120  # s, each tar, untar, find one-off and psql query: local volume and socket work
_MAJOR: Final = re.compile(r"v?(\d+)\.\d+")


# Trust (FR-CLI-16) ----------------------------------------------------------------------------------------


def _roots_dir(ctx: Ctx) -> Path:
    return ctx.run_dir / "trusted-roots"


GATE_TIMEOUT_S: Final = 5.0


def gate_status(url: str, roots: Sequence[str | Path]) -> int | None:
    """FR-CLI-22: the HTTP status of a cookie-less GET of the dashboard, the certificate and host name
    verified against `roots` (PEM root files: those `reader trust` recorded and Caddy's current one); a
    root file that does not load is skipped. None when there is no answer or the certificate does not
    verify. Redirects are not followed (a 3xx counts as an open gate)."""
    import urllib.error
    import urllib.request

    context = ssl.create_default_context()  # CERT_REQUIRED and check_hostname
    for root in roots:
        try:
            context.load_verify_locations(cafile=str(root))
        except (ssl.SSLError, OSError, ValueError):
            continue  # a bad or unreadable root is no root

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: Any, **kwargs: Any) -> None:
            return None

    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), NoRedirect)
    try:
        with opener.open(url, timeout=GATE_TIMEOUT_S) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)
    except (OSError, ValueError):
        return None


def _login_keychain(ctx: Ctx) -> str:
    home = ctx.env.get("HOME") or str(Path.home())
    return str(Path(home) / "Library" / "Keychains" / "login.keychain-db")


def caddy_root_pem(ctx: Ctx, project: str) -> bytes | None:
    """Caddy's public root certificate from the running stack, or None (stack down, not PEM)."""
    exported = docker(ctx, "docker", "compose", "-p", project, "exec", "-T", "caddy", "cat", ROOT_CRT)
    if exported is None or exported.code != 0 or not exported.out.strip():
        return None
    return exported.out


DOCTOR_ROOT: Final = "doctor-root.pem"


def valid_root(pem: bytes) -> bool:
    """A PEM certificate that loads as a verification root (a bad or truncated one is no root)."""
    try:
        text = pem.decode("ascii")
        ssl.PEM_cert_to_DER_cert(text)
        ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=text)
    except (UnicodeDecodeError, ValueError, ssl.SSLError):
        return False
    return True


def doctor_roots(ctx: Ctx, project: str | None, recorded: Sequence[Path]) -> list[Path]:
    """FR-CLI-22: the roots `reader trust` recorded plus Caddy's current public root, saved under run/ for
    this check only (never added to a keychain)."""
    roots = list(recorded)
    pem = caddy_root_pem(ctx, project) if project else None
    if pem is not None and valid_root(pem):
        ensure_run_dir(ctx)
        path = ctx.run_dir / DOCTOR_ROOT
        path.unlink(missing_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(pem)
        roots.append(path)
    return roots


def trust_root(ctx: Ctx, project: str) -> int:
    pem = caddy_root_pem(ctx, project)
    if pem is None:
        raise CliError("trust_failed", "cannot read Caddy's root certificate (is the stack up?)")
    der: bytes | None
    try:
        der = ssl.PEM_cert_to_DER_cert(pem.decode("ascii"))
    except ValueError:
        der = None
    if der is None:
        raise CliError("trust_failed", "Caddy's root certificate is not PEM")
    sha1 = hashlib.sha1(der).hexdigest().upper()  # noqa: S324 - the keychain's certificate identifier
    ensure_run_dir(ctx)
    roots = _roots_dir(ctx)
    roots.mkdir(mode=0o700, exist_ok=True)
    path = roots / f"{sha1}.pem"
    path.write_bytes(pem)
    added = docker(
        ctx,
        "security",
        "add-trusted-cert",
        "-r",
        "trustRoot",
        "-p",
        "ssl",
        "-k",
        _login_keychain(ctx),
        str(path),
    )
    if added is None or added.code != 0:
        raise CliError("trust_failed", "security add-trusted-cert failed")
    print(f"trusted Caddy's local root {sha1} for TLS (remove with `reader down --untrust`)", file=ctx.out)
    return 0


def _still_trusted(ctx: Ctx, sha1s: set[str]) -> set[str] | None:
    """The recorded SHA-1s still present in the user trust settings; None when the export fails."""
    ensure_run_dir(ctx)
    export = ctx.run_dir / "trust-settings.plist"
    export.unlink(missing_ok=True)
    result = docker(ctx, "security", "trust-settings-export", str(export))
    try:
        if result is None:
            return None
        if result.code != 0 and not export.exists():
            return set() if b"no trust settings" in (result.out + result.err).lower() else None
        data: Any = plistlib.loads(export.read_bytes())
    except OSError, plistlib.InvalidFileException:
        return None
    finally:
        export.unlink(missing_ok=True)
    trusted = data.get("trustList", {}) if isinstance(data, dict) else {}
    return {sha for sha in sha1s if sha in {str(k).upper() for k in trusted}}


def untrust_all(ctx: Ctx) -> int:
    roots = sorted(_roots_dir(ctx).glob("*.pem")) if _roots_dir(ctx).exists() else []
    if not roots:
        return 0
    for pem in roots:
        docker(ctx, "security", "remove-trusted-cert", str(pem))
        docker(ctx, "security", "delete-certificate", "-t", "-Z", pem.stem)
    remaining = _still_trusted(ctx, {pem.stem for pem in roots})
    if remaining is None or remaining:
        print(
            "error: a recorded root is still trusted (or the check failed); see Keychain Access", file=ctx.err
        )
        return 1
    for pem in roots:
        pem.unlink()
    print(f"removed {len(roots)} trusted root(s)", file=ctx.out)
    return 0


@guarded
def cmd_trust(ctx: Ctx, config_path: Path) -> int:
    return trust_root(ctx, load(ctx, config_path).model.name)


# Doctor (FR-CLI-15) ---------------------------------------------------------------------------------------


def compose_major(text: str) -> int | None:
    match = _MAJOR.match(text.strip())
    return int(match.group(1)) if match else None


@guarded
def cmd_doctor(ctx: Ctx, config_path: Path, as_json: bool = False) -> int:
    config = load_config(config_path)  # a configuration error exits 2 before any check runs
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    info = docker(ctx, "docker", "info", "--format", "{{.ServerVersion}}")
    check("docker", info is not None and info.code == 0, "" if info and info.code == 0 else "unreachable")
    composed = docker(ctx, "docker", "compose", "version", "--short")
    major = (
        compose_major(composed.out.decode("utf-8", "replace")) if composed and composed.code == 0 else None
    )
    check("compose", major is not None and major >= 2, f"major {major} (measured on 5.6.1, REF-CLI-7)")
    backend = type(ctx.keyring).__module__
    lookup = True
    try:
        ctx.keyring.get_password(SYSTEM_SERVICE, "postgres")
    except Exception:  # any failure of the lookup is the finding
        lookup = False
    check("keyring", lookup and backend == "keyring.backends.macOS", f"backend {backend}")
    project: Project | None = None
    try:
        # FR-CLI-5: the profiles `reader up` would choose now, so provider references are checked before
        # the first `up` with agents.
        project = load(ctx, config_path, choose_profiles(ctx, config), config)
    except (CliError, OSError) as error:
        check("config", False, error.code if isinstance(error, CliError) else "unreadable")
    if project is not None:
        check("config", True, f"{len(config.users)} user(s)")
        _doctor_secrets(ctx, project, check)
        _doctor_stack(ctx, project.model, check)
    roots = sorted(_roots_dir(ctx).glob("*.pem")) if _roots_dir(ctx).exists() else []
    if roots:
        trusted = _still_trusted(ctx, {p.stem for p in roots})
        check("trust", trusted == {p.stem for p in roots}, f"{len(roots)} recorded root(s)")
    # FR-CLI-22: with the profile chosen, the dashboard answers 401 without a cookie (the gate is in front).
    # SRV-OBS FR-OBS-19: the Jaeger site, behind the same gate, is checked the same way.
    if config.observability.enabled:
        gate_roots = doctor_roots(ctx, project.model.name if project is not None else None, roots)
        for name, url in (("observability", TRACES_URL), ("jaeger", JAEGER_URL)):
            status = gate_status(url, gate_roots) if gate_roots else None
            if not gate_roots:
                check(name, False, "unreachable: no Caddy root (stack down?)")
            elif status == 401:
                check(name, True, "gate ok")
            elif status is None:
                check(name, False, "unreachable")
            elif 200 <= status < 400:
                check(name, False, "gate open")
            else:
                check(name, False, f"status {status}")
    else:
        check("observability", True, "off")
        check("jaeger", True, "off")
    obs_on = project is not None and config.observability.enabled
    obs_lines = obs_doctor_lines(ctx, project.model) if project is not None and obs_on else []
    ok = all(c["ok"] for c in checks)
    if as_json:  # FR-CLI-24 (e): the obs lines as informational checks; the array's shape stays
        obs = [{"name": "obs", "ok": True, "detail": line} for line in obs_lines]
        print(json.dumps([*checks, *obs]), file=ctx.out)
    else:
        for c in checks:
            print(f"{'ok  ' if c['ok'] else 'FAIL'} {c['name']}: {c['detail']}".rstrip(": "), file=ctx.out)
        for line in obs_lines:  # FR-CLI-24: the observability stack's own state
            print(line, file=ctx.out)
    return 0 if ok else 1


LAST_JOBS_SQL: Final = (  # a constant: nothing user-supplied enters the query
    "SELECT kind || '=' || to_char(max(finished_at) AT TIME ZONE 'UTC', "
    "'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') FROM job_keys WHERE kind IN ('obs.retention', 'obs.setup') "
    "AND outcome = 'succeeded' GROUP BY kind"
)
_DATA: Final = obs_store.DATA
STORE_FACTS: Final = (
    f"test -f {_DATA}/mlflow.db || exit 0; python -c \"import sqlite3, os; "  # noqa: S608 (a shell constant)
    f"c = sqlite3.connect('file:{_DATA}/mlflow.db?mode=ro', uri=True); "
    "print(c.execute('pragma journal_mode').fetchone()[0]); "
    "print(c.execute('select version_num from alembic_version').fetchone()[0]); "
    f"print('fresh' if not os.path.exists('{_DATA}/backup/pre-migrate.db') else 'migrated')\""
)


def obs_doctor_lines(ctx: Ctx, model: Rendered, *, store_size: bool = True) -> list[str]:
    """FR-CLI-24: one line each for mlflow, rotel and jaeger from `docker inspect`, the store's size from
    `docker system df -v` (host side, no worker mount; skipped with `store_size=False`, `reader status`
    without `--obs`: that call takes seconds), its journal mode, and the obs jobs' last runs."""
    names = [f"{model.name}-{m}-1" for m in obs_store.MEMBERS if m in model.services]
    inspected = docker(ctx, "docker", "inspect", *names) if names else None
    try:
        inspect = json.loads(inspected.out) if inspected is not None and inspected.code == 0 else []
    except ValueError:
        inspect = []
    df = docker(ctx, "docker", "system", "df", "-v", "--format", "json") if store_size else None
    df_text = df.out.decode("utf-8", "replace") if df is not None and df.code == 0 else "{}"
    journal, fresh = None, None
    volume = _obs_volume(ctx, model)
    if volume is not None:
        code, out = _obs_run(ctx)(obs_store.one_off(model.image("mlflow"), volume, STORE_FACTS), 60.0)
        facts = out.strip().splitlines()[-3:] if code == 0 else []
        if len(facts) == 3:
            journal, revision, state = facts
            fresh = obs_store.fresh_line(revision) if state == "fresh" else None
    jobs = docker(ctx, *_psql(model.name, LAST_JOBS_SQL))
    last: dict[str, str] = {}
    if jobs is not None and jobs.code == 0:
        for row in jobs.out.decode("utf-8", "replace").splitlines():
            kind, _, when = row.partition("=")
            if kind in obs_store.TRACKED_JOBS and when:
                last[kind] = when
    lines = obs_store.doctor_lines(inspect=inspect, df=df_text, journal_mode=journal, last_jobs=last)
    return [*lines, fresh] if fresh else lines


@guarded
def cmd_restore_obs_premigrate(ctx: Ctx, config_path: Path, force: bool) -> int:
    """FR-CLI-24 / SRV-OBS FR-OBS-29: put the pre-migration backup back after a failed migration (stack down).
    Exit codes 0..7 (obs_store.PREMIGRATE_CODES)."""
    project = load(ctx, config_path)
    model = project.model
    volume = _obs_volume(ctx, model)
    if volume is None:
        print("error: there is no MLflow store volume", file=ctx.err)
        return obs_store.PREMIGRATE_CODES["no_backup"]
    code, out = obs_store.restore_premigrate(volume=volume, image=model.image("mlflow"), force=force,
                                             run=_obs_run(ctx))
    print(out.strip(), file=ctx.out if code == 0 else ctx.err)
    return code


def _doctor_secrets(ctx: Ctx, project: Project, check: Any) -> None:
    plan = make_plan(project.model, project.config)
    registry = host_resolvers(ctx.keyring, ctx.runner, ctx.env)
    failures: list[str] = []
    for ref in (r for r in plan.sources.values() if r is not None):
        try:
            resolve(ref, registry)  # the value is discarded at once
        except SecretResolutionError as error:
            failures.append(f"{ref}: {error.code}")
    check("secrets", not failures, "; ".join(failures))
    hostfiles = [p.token for u in project.config.users for p in u.providers if p.token.scheme == "hostfile"]
    bad: list[str] = []
    for ref in hostfiles:
        try:
            resolve(ref, registry)
        except SecretResolutionError as error:
            bad.append(f"{ref}: {error.code}")
    check("hostfile", not bad, "; ".join(bad))


def _doctor_stack(ctx: Ctx, model: Rendered, check: Any) -> None:
    item = True
    try:
        item = ctx.keyring.get_password(SYSTEM_SERVICE, "postgres") is not None
    except Exception:  # reported by the keyring check
        item = False
    volume = docker(ctx, "docker", "volume", "inspect", model.pgdata_volume)
    exists = volume is not None and volume.code == 0
    detail = (
        "item present"
        if item
        else "volume exists without the item"
        if exists
        else "not generated yet (fresh install)"
    )
    check("db-password", item or not exists, detail)
    services = ps(ctx, model.name) or []
    running = any(s.get("State") == "running" for s in services)
    check("watcher", watcher_running(ctx) or not running, "running" if watcher_running(ctx) else "stopped")


# Backup and restore (FR-CLI-17, FR-CLI-18) ----------------------------------------------------------------


OBS_VOLUME: Final = "obs_mlflow"  # backed up through SQLite's backup API, never as a tar (SRV-OBS FR-OBS-30)
OBS_FILES: Final = ("obs_mlflow.db", "obs_mlflow.artifacts.tar", "obs_mlflow.store.version")


def backup_volumes(model: Rendered) -> list[str]:
    """Volumes labelled include and tarred, except pgdata (dumped with pg_dump) and obs_mlflow (FR-OBS-30)."""
    keys: list[str] = []
    for key, body in sorted(model.volumes.items()):
        labels = body.get("labels") if isinstance(body, dict) else None
        included = isinstance(labels, dict) and labels.get(BACKUP_LABEL) == "include"
        if key not in ("pgdata", OBS_VOLUME) and included:
            keys.append(key)
    return keys


def _obs_volume(ctx: Ctx, model: Rendered) -> str | None:
    """The obs_mlflow volume's name when the model and Docker have it (the profile may never have run)."""
    if OBS_VOLUME not in model.volumes or "mlflow" not in model.services:
        return None
    name = _volume_name(model, OBS_VOLUME)
    found = docker(ctx, "docker", "volume", "inspect", name)
    return name if found is not None and found.code == 0 else None


def _obs_backup(ctx: Ctx, model: Rendered) -> dict[str, bytes]:
    """FR-OBS-30: the store through the backup API, the artifacts, and store.version beside them."""
    volume = _obs_volume(ctx, model)
    if volume is None:
        return {}
    with tempfile.TemporaryDirectory(prefix="reader-obs-backup-") as tmp:
        try:
            obs_store.backup(
                volume=volume, image=model.image("mlflow"), directory=Path(tmp), run=_obs_run(ctx)
            )
        except RuntimeError as error:
            raise CliError("backup_failed", "the MLflow store backup failed") from error
        return {n: (Path(tmp) / n).read_bytes() for n in OBS_FILES if (Path(tmp) / n).is_file()}


def _migrate_once(ctx: Ctx, project: Project) -> None:
    """After a restore, mlflow-migrate runs (a fresh one-shot through `docker compose run --rm`, so a
    never-created container cannot skip it) and must exit 0 before mlflow starts again (FR-OBS-29, -30;
    OPS-CLI FR-CLI-24). Any other exit raises with the code's message; the caller leaves mlflow stopped."""
    extra = tuple(sorted({*project.profiles, "observability"}))
    argv = compose_argv(ctx, "run", "--rm", "--no-deps", "mlflow-migrate", extra=extra)
    ran = docker(ctx, *argv, stdin=project.override, timeout=ONE_OFF_TIMEOUT * 5)
    exit_code = ran.code if ran is not None else -1
    if exit_code != 0:
        raise CliError("restore_failed", obs_store.migrate_message(exit_code))


def _obs_restore(ctx: Ctx, model: Rendered, files: dict[str, bytes]) -> None:
    """FR-OBS-30, stack down: store.version and the markers first, the file through the backup API,
    store.version from the backup; mlflow-migrate then runs before mlflow starts."""
    volume = _obs_volume(ctx, model)
    if volume is None or "obs_mlflow.db" not in files:
        return
    with tempfile.TemporaryDirectory(prefix="reader-obs-restore-") as tmp:
        for name in OBS_FILES:
            if name in files:
                (Path(tmp) / name).write_bytes(files[name])
        try:
            obs_store.restore(
                directory=Path(tmp), volume=volume, image=model.image("mlflow"), run=_obs_run(ctx)
            )
        except RuntimeError as error:
            raise CliError("restore_failed", "the MLflow store restore failed") from error


def _volume_name(model: Rendered, key: str) -> str:
    name = model.volumes.get(key, {}).get("name")
    if not isinstance(name, str):
        raise CliError("bad_model", f"volume {key} has no rendered name")
    return name


def _one_off(image: str, user: str, volume: str, *command: str, read_only: bool = True) -> list[str]:
    """A hardened one-off on one volume. `--entrypoint ""`: the server image's entrypoint waits for
    /run/secrets/db-password, which a one-off never gets (measured: it hangs without this flag)."""
    mount = f"{volume}:/v:ro" if read_only else f"{volume}:/v"
    interactive = [] if read_only else ["-i"]
    return [
        "docker",
        "run",
        "--rm",
        *interactive,
        "--entrypoint",
        "",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--user",
        user,
        "-v",
        mount,
        image,
        *command,
    ]


def _postgres(project: str, *command: str) -> list[str]:
    return ["docker", "compose", "-p", project, "exec", "-T", "postgres", *command]


def _psql(project: str, sql: str) -> list[str]:
    return _postgres(project, "psql", "-U", "postgres", "-d", "postgres", "-At", "-c", sql)


def _capture(
    ctx: Ctx, argv: list[str], what: str, code: str, timeout: float, stdin: bytes | None = None
) -> bytes:
    result = docker(ctx, *argv, stdin=stdin, timeout=timeout)
    if result is None or result.code != 0:
        raise CliError(code, f"{what} failed or timed out")
    return result.out


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
    finally:
        os.close(fd)


def _services(ctx: Ctx, project: str, action: str, names: list[str]) -> None:
    if names:
        result = docker(ctx, "docker", "compose", "-p", project, action, *names)
        if result is None or result.code != 0:
            raise CliError(f"{action}_failed", f"docker compose {action} failed")


def _reader_version() -> str:
    try:
        return version("reader")
    except PackageNotFoundError:
        return "unknown"


@guarded
def cmd_backup(ctx: Ctx, config_path: Path, directory: Path) -> int:
    project = load(ctx, config_path)
    model = project.model
    dest = Path(os.path.abspath(directory))
    if in_git_work_tree(dest if dest.exists() else dest.parent):
        raise CliError("backup_in_git", "the backup directory is inside a git work tree")
    if not watcher_running(ctx):
        raise CliError("watcher_stopped", "the watcher is not running; run `reader up` first")
    keys = backup_volumes(model)
    users = {key: volume_user(model, key) for key in keys}
    image = model.image("api")
    writers = [s for s in WRITERS if s in model.services]
    files: dict[str, bytes] = {}
    failed = "backup_failed"
    try:
        _services(ctx, model.name, "stop", writers)
        dump = _postgres(model.name, "pg_dump", "-Fc", "-U", "postgres", "postgres")
        files["db.dump"] = _capture(ctx, dump, "pg_dump", failed, DUMP_TIMEOUT)
        for key in keys:
            files[f"{key}.tar"] = _capture(
                ctx,
                _one_off(image, users[key], _volume_name(model, key), "tar", "-C", "/v", "-cf", "-", "."),
                f"tar of {key}",
                failed,
                ONE_OFF_TIMEOUT,
            )
        files.update(_obs_backup(ctx, model))  # SRV-OBS FR-OBS-30: online, through SQLite's backup API
        version_sql = _psql(model.name, "SHOW server_version")
        server = _capture(ctx, version_sql, "postgres version", failed, ONE_OFF_TIMEOUT).decode().strip()
    finally:
        _services(ctx, model.name, "start", writers)
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, data in files.items():
        _write_private(dest / name, data)
    manifest = {
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "reader": _reader_version(),
        "postgres": server,
        "files": [
            {"name": n, "sha256": hashlib.sha256(d).hexdigest(), "bytes": len(d)} for n, d in files.items()
        ],
    }
    _write_private(dest / "manifest.json", json.dumps(manifest, indent=2).encode())
    print(f"backup written to {dest}", file=ctx.out)
    return 0


def _read_backup(source: Path) -> dict[str, bytes]:
    """The manifest's files, each verified by sha256; anything malformed is `bad_backup`."""
    files: dict[str, bytes] = {}
    mismatch: str | None = None
    try:
        manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
        for entry in manifest["files"]:
            name, digest = str(entry["name"]), str(entry["sha256"])
            if "/" in name or name.startswith("."):
                raise ValueError(name)
            data = (source / name).read_bytes()
            if hashlib.sha256(data).hexdigest() != digest:
                mismatch = name
                break
            files[name] = data
    except OSError, ValueError, KeyError, TypeError:
        files = {}
    if mismatch is not None:
        raise CliError("checksum_mismatch", f"{mismatch} does not match the manifest")
    if "db.dump" not in files:
        raise CliError("bad_backup", "the backup's manifest or files are missing or malformed")
    return files


@guarded
def cmd_restore(ctx: Ctx, config_path: Path, directory: Path) -> int:
    project = load(ctx, config_path)
    model = project.model
    if not watcher_running(ctx):
        raise CliError("watcher_stopped", "the watcher is not running; run `reader up` first")
    source = Path(directory)
    files = _read_backup(source)
    keys = [k for k in backup_volumes(model) if f"{k}.tar" in files]
    users = {key: volume_user(model, key) for key in keys}
    image = model.image("api")
    failed = "restore_failed"
    rows = _capture(ctx, _psql(model.name, ROWS_SQL), "row count", failed, ONE_OFF_TIMEOUT).decode().strip()
    if rows != "0":
        raise CliError(
            "database_not_empty", "the database holds rows; restore needs a freshly initialised pgdata"
        )
    if "blobs" in model.volumes:
        empty_check = _one_off(
            image, volume_user(model, "blobs"), _volume_name(model, "blobs"), "sh", "-c", EMPTY_CHECK
        )
        if _capture(ctx, empty_check, "blobs check", failed, ONE_OFF_TIMEOUT).strip():
            raise CliError("blobs_not_empty", "the blobs volume is not empty")
    obs = "obs_mlflow.db" in files and _obs_volume(ctx, model) is not None
    stopped = [s for s in (*WRITERS, "caddy", *(("mlflow",) if obs else ())) if s in model.services]
    migrated = not obs  # FR-CLI-24: mlflow starts again only after mlflow-migrate exited 0
    try:
        _services(ctx, model.name, "stop", stopped)
        _capture(
            ctx,
            _postgres(model.name, "pg_restore", "--clean", "--if-exists", "-U", "postgres", "-d", "postgres"),
            "pg_restore",
            failed,
            DUMP_TIMEOUT,
            stdin=files["db.dump"],
        )
        for key in keys:
            _capture(
                ctx,
                _one_off(
                    image, users[key], _volume_name(model, key), "sh", "-c", RESTORE_SCRIPT, read_only=False
                ),
                f"untar of {key}",
                failed,
                ONE_OFF_TIMEOUT,
                stdin=files[f"{key}.tar"],
            )
        if obs:
            _obs_restore(ctx, model, files)
            _migrate_once(ctx, project)
            migrated = True
    finally:
        _services(ctx, model.name, "start", [s for s in stopped if s != "mlflow" or migrated])
    print(f"restored from {source}", file=ctx.out)
    return 0
