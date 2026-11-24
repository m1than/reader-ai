"""MLflow's store on the host side (OPS-CLI FR-CLI-24, SRV-OBS FR-OBS-29 to FR-OBS-32).

The doctor and status lines for mlflow, rotel and jaeger; the obs_mlflow part of `reader backup` and
`reader restore` (always through SQLite's backup API, with store.version beside the copy);
`reader restore-obs-premigrate`; and `reader up`'s message for each `mlflow-migrate` exit code.

Every docker call is a hardened one-off on the obs_mlflow volume (the store's uid 10004, read-only root,
no network, no capabilities). `run` executes one argv with a timeout and returns (exit code, output); the
default runs it with subprocess, and the CLI commands pass their own runner."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

Run = Callable[[list[str], float], tuple[int, str]]

DATA: Final = "/var/lib/mlflow"
STORE_UID: Final = "10004:10004"
STORE_BYTES_MAX: Final = 4 * 1024**3  # FR-OBS-31, structural
STORE_WARN_RATIO: Final = 0.8
ONE_OFF_TIMEOUT_S: Final = 300.0
MEMBERS: Final = ("mlflow", "rotel", "jaeger")
TRACKED_JOBS: Final = ("obs.retention", "obs.setup")

# FR-CLI-24: restore-obs-premigrate's exit codes; mlflow-migrate's own are 10..13 (FR-OBS-29).
PREMIGRATE_CODES: Final = {
    "restored": 0, "stack_up": 2, "no_backup": 3, "copy_failed": 4, "marker_failed": 5,
    "not_in_progress": 6, "no_version": 7,
}
MIGRATE_CODES: Final = {
    10: "obs_migration_incomplete",
    11: "obs_restored_for_other_version",
    12: "obs_migration_failed",
    13: "obs_migration_finish_failed",
}


def _subprocess_run(argv: list[str], timeout: float) -> tuple[int, str]:
    # argv is built here from constants and docker object names, never from user text
    proc = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=timeout)  # noqa: S603
    return proc.returncode, proc.stdout + proc.stderr


def one_off(image: str, volume: str, script: str, *mounts: str, user: str = STORE_UID) -> list[str]:
    """A hardened one-off shell on the volume (plus extra `-v` mounts)."""
    extra = [a for m in mounts for a in ("-v", m)]
    tmpfs = "/tmp:uid=" + user.split(":")[0]  # noqa: S108 (the container's own tmpfs)
    return [
        "docker", "run", "--rm", "--user", user, "--read-only", "--tmpfs", tmpfs, "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--network", "none", "-e", "HOME=/tmp",
        "-v", f"{volume}:{DATA}", *extra, "--entrypoint", "sh", image, "-c", script,
    ]


# --- doctor and status lines ---------------------------------------------------------------------

_UNITS: Final = {"B": 1, "kB": 10**3, "KB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12,
                 "KiB": 2**10, "MiB": 2**20, "GiB": 2**30, "TiB": 2**40}


def size_bytes(text: str) -> int | None:
    """`docker system df -v`'s human size (decimal units, as Docker prints them; binary too)."""
    m = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)\s*", text)
    if not m or m.group(2) not in _UNITS:
        return None
    return int(float(m.group(1)) * _UNITS[m.group(2)])


def store_size(df: str) -> str | None:
    """The obs_mlflow volume's size from `docker system df -v --format json`."""
    try:
        volumes = json.loads(df).get("Volumes") or []
    except ValueError, AttributeError:
        return None
    for v in volumes:
        if isinstance(v, dict) and str(v.get("Name", "")).endswith("obs_mlflow"):
            size = v.get("Size")
            return size if isinstance(size, str) else None
    return None


def _member(record: Mapping[str, Any]) -> str | None:
    name = str(record.get("Name", "")).lstrip("/")
    for member in MEMBERS:
        if re.search(rf"(^|[-_]){member}([-_][0-9]+)?$", name):
            return member
    return None


def doctor_lines(
    *, inspect: Sequence[Mapping[str, Any]], df: str, journal_mode: str | None,
    last_jobs: Mapping[str, str],
) -> list[str]:
    """One line per stack member in `inspect` (mlflow, rotel, jaeger order), the obs jobs' last runs,
    then the `obs_store_large` warning at 80 % of 4 GiB."""
    by_member = {m: r for r in inspect if (m := _member(r)) is not None}
    lines: list[str] = []
    size = store_size(df)
    for member in MEMBERS:
        record = by_member.get(member)
        if record is None:
            continue
        state = record.get("State") or {}
        image = (record.get("Config") or {}).get("Image", "?")
        parts = [f"{image} {'running' if state.get('Running') else 'not running'}",
                 f"restarts {int(record.get('RestartCount') or 0)}"]
        health = (state.get("Health") or {}).get("Status")
        if health:
            parts.append(f"health {health}")
        if member == "mlflow":
            parts.append(f"store {size or 'unknown'}")
            parts.append(f"journal {journal_mode or 'unknown'}")
        lines.append(f"obs {member}: " + ", ".join(parts))
    if last_jobs:
        runs = [f"{k} {last_jobs[k]}" for k in TRACKED_JOBS if k in last_jobs]
        lines.append("obs jobs: " + ", ".join(runs))
    measured = size_bytes(size) if size else None
    if measured is not None and measured >= STORE_WARN_RATIO * STORE_BYTES_MAX:
        lines.append(f"obs_store_large: the MLflow store is {size}, at least 80 % of 4 GiB (FR-OBS-31)")
    return lines


def fresh_line(revision: str) -> str:
    return f"obs mlflow: fresh store, schema {revision}"


def migrate_message(code: int, restored_for: str | None = None) -> str:
    """`reader up`'s message when mlflow-migrate exits non-zero (FR-OBS-29's codes 10..13)."""
    name = MIGRATE_CODES.get(code, f"obs_migration_exit_{code}")
    if code == 11:
        version = restored_for or "another version"
        return (f"{name}: the MLflow store was restored for MLflow {version}; revert the image pin in "
                "deploy/compose.yaml (your own git change) and run `reader up` again")
    if code == 10:
        return (f"{name}: a previous migration of the MLflow store did not finish; run "
                "`reader restore-obs-premigrate`")
    return (f"{name}: the MLflow store's migration failed; run `reader restore-obs-premigrate` "
            "with the stack down")


# --- backup and restore (FR-OBS-30) --------------------------------------------------------------

# The copy leaves WAL mode (the backup API copies the header's WAL flag), so it opens from a read-only mount;
# mlflow-migrate's step (4) sets WAL again on the restored store.
_BACKUP_PY: Final = (
    f"import sqlite3; s = sqlite3.connect('file:{DATA}/mlflow.db?mode=ro', uri=True); "
    "d = sqlite3.connect('/out/obs_mlflow.db'); s.backup(d); "
    "d.execute('pragma journal_mode=delete'); d.close(); s.close()"
)
_RESTORE_PY: Final = (
    "import sqlite3; s = sqlite3.connect('file:/in/obs_mlflow.db?mode=ro&immutable=1', uri=True); "
    f"d = sqlite3.connect('{DATA}/mlflow.db.restore'); s.backup(d); d.close(); s.close()"
)
_ARTIFACTS_OUT: Final = (
    f"if [ -d {DATA}/artifacts ]; then tar -C {DATA} -cf /out/obs_mlflow.artifacts.tar artifacts; fi"
)
_ARTIFACTS_IN: Final = (
    f"if [ -f /in/obs_mlflow.artifacts.tar ]; then tar -C {DATA} -xf /in/obs_mlflow.artifacts.tar; fi"
)
_VERSION_OUT: Final = (
    f"if [ -f {DATA}/store.version ]; then cp {DATA}/store.version /out/obs_mlflow.store.version; fi"
)
_VERSION_IN: Final = (
    f"if [ -f /in/obs_mlflow.store.version ]; then cat /in/obs_mlflow.store.version "
    f"> {DATA}/store.version; fi"
)
_MARKERS: Final = f"{DATA}/backup/in-progress {DATA}/backup/fresh {DATA}/backup/restored-for"


def backup_steps(*, volume: str, image: str, directory: Path) -> list[list[str]]:
    out = f"{Path(directory).resolve()}:/out"
    return [
        one_off(image, volume, f'python -c "{_BACKUP_PY}"', out),
        one_off(image, volume, _ARTIFACTS_OUT, out),
        one_off(image, volume, _VERSION_OUT, out),
    ]


def restore_steps(*, directory: Path, volume: str, image: str) -> list[list[str]]:
    """Stack down. Order (FR-OBS-30): store.version goes first, then the replaced store's markers, then
    the file through the backup API, then store.version from the backup (absent when it has none)."""
    src = f"{Path(directory).resolve()}:/in:ro"
    # Copy first (into a temp file in the volume), then drop the old WAL files, then move into place: a
    # failed copy leaves the old store and its WAL untouched (code review round 1, B3).
    swap = (f'python -c "{_RESTORE_PY}" && rm -f {DATA}/mlflow.db-wal {DATA}/mlflow.db-shm && '
            f"mv {DATA}/mlflow.db.restore {DATA}/mlflow.db")
    return [
        one_off(image, volume, f"rm -f {DATA}/store.version", src),
        one_off(image, volume, f"rm -f {_MARKERS}", src),
        one_off(image, volume, swap, src),
        one_off(image, volume, _ARTIFACTS_IN, src),
        one_off(image, volume, _VERSION_IN, src),
    ]


def _run_all(steps: list[list[str]], run: Run, what: str) -> None:
    for argv in steps:
        code, out = run(argv, ONE_OFF_TIMEOUT_S)
        if code != 0:
            raise RuntimeError(f"{what} failed (exit {code}): {out[-300:]}")


def backup(*, volume: str, image: str, directory: Path, run: Run | None = None) -> None:
    Path(directory).mkdir(parents=True, exist_ok=True)
    steps = backup_steps(volume=volume, image=image, directory=directory)
    _run_all(steps, run or _subprocess_run, "obs backup")


def restore(*, directory: Path, volume: str, image: str, run: Run | None = None) -> None:
    steps = restore_steps(directory=directory, volume=volume, image=image)
    _run_all(steps, run or _subprocess_run, "obs restore")


# --- restore-obs-premigrate (FR-OBS-29) ----------------------------------------------------------

_PREMIGRATE_SH: Final = """
set -u
D={data}; B=$D/backup
if [ ! -f $B/in-progress ] && [ "{force}" != 1 ]; then echo "no migration in progress" >&2; exit 6; fi
if [ ! -f $B/pre-migrate.db ]; then echo "no pre-migrate.db" >&2; exit 3; fi
V=$(cat $B/pre-migrate.version 2>/dev/null)
if [ -z "$V" ]; then echo "no pre-migrate.version: the backup's MLflow version is unknown" >&2; exit 7; fi
echo "restoring the MLflow store migrated from MLflow $V"
python -c "import sqlite3; s = sqlite3.connect('file:$B/pre-migrate.db?mode=ro', uri=True); \
d = sqlite3.connect('$D/mlflow.db.restore'); s.backup(d); d.close(); s.close()" \
  || {{ rm -f $D/mlflow.db.restore; exit 4; }}
rm -f $D/mlflow.db-wal $D/mlflow.db-shm || exit 4
mv $D/mlflow.db.restore $D/mlflow.db || exit 4
printf '%s\\n' "$V" > $D/store.version || exit 5
printf '%s\\n' "$V" > $B/restored-for || exit 5
rm -f $B/in-progress || exit 5
echo "restored: MLflow $V; mlflow-migrate of another version stops (exit 11) until the image is $V"
"""


def volume_in_use(volume: str, run: Run) -> bool:
    code, out = run(["docker", "ps", "-q", "--filter", f"volume={volume}"], 60.0)
    return code == 0 and bool(out.strip())


def restore_premigrate(
    *, volume: str, image: str, force: bool, run: Run | None = None
) -> tuple[int, str]:
    """`reader restore-obs-premigrate`: (exit code, output); 2 while a container uses the volume."""
    runner = run or _subprocess_run
    if volume_in_use(volume, runner):
        return PREMIGRATE_CODES["stack_up"], "the stack is up: run `reader down` first"
    script = _PREMIGRATE_SH.format(data=DATA, force=1 if force else 0)
    return runner(one_off(image, volume, script), ONE_OFF_TIMEOUT_S)
