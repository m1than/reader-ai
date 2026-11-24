"""Run records and stale cleanup (SRV-INGEST FR-INGEST-1, FR-INGEST-16).

A record names its material and work directory before the directory exists, so no work directory is ever
anonymous. At the next start, under the material's lock, records of that material are stale; a recorded child
session is killed only if the process is alive, started at the recorded time, and has its cwd in the record's
work directory, because a pid can be reused by an unrelated process.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Final, Protocol

LOCK_RETRY_S: Final = 0.2


def try_flock(fd: int) -> bool:
    """An exclusive non-blocking flock, retried once after 0.2 s: `reader status` probes the ingest locks with
    a short shared lock (OPS-CLI FR-CLI-23), which must not make a real run read `busy`."""
    for attempt in (0, 1):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if attempt == 0:
                time.sleep(LOCK_RETRY_S)
    return False


class Probe(Protocol):
    def alive(self, pid: int) -> bool: ...
    def start_time(self, pid: int) -> str | None: ...
    def cwd(self, pid: int) -> str | None: ...
    def killpg(self, pid: int) -> None: ...


def _write(path: Path, record: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(record, handle, sort_keys=True)
    os.replace(temporary, path)


def new_run(run_dir: Path, work_root: Path, material_sha256: str, upload_id: str | None = None) -> str:
    """Write the record `{material_sha256, work}` (plus `upload_id` in watch mode, FR-INGEST-21), then create
    the 0700 work directory; returns its name."""
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = secrets.token_hex(8)
    work = work_root / name
    record: dict[str, Any] = {"material_sha256": material_sha256, "work": str(work)}
    if upload_id is not None:
        record["upload_id"] = upload_id
    path, made = run_dir / f"{name}.json", False
    try:
        _write(path, record)
        work.mkdir(mode=0o700)
        made = True
        os.chmod(work, 0o700)
    except BaseException:  # leave nothing half-made: the record, its temporary file, a directory we created
        for leftover in (path, path.with_name(f".{path.name}.tmp")):
            with contextlib.suppress(OSError):
                leftover.unlink()
        if made:
            shutil.rmtree(work, ignore_errors=True)
        raise
    return name


def set_child(run_dir: Path, name: str, sid: int, start: str) -> None:
    path = run_dir / f"{name}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(child_sid=sid, child_start=start)
    _write(path, record)


def end_run(run_dir: Path, work_root: Path, name: str) -> None:
    shutil.rmtree(work_root / name, ignore_errors=True)
    with contextlib.suppress(FileNotFoundError):
        (run_dir / f"{name}.json").unlink()


def _inside(path: Path, root: Path) -> bool:
    try:
        return Path(os.path.realpath(path)).is_relative_to(os.path.realpath(root)) and Path(
            os.path.realpath(path)
        ) != Path(os.path.realpath(root))
    except OSError:
        return False


def cleanup_stale(run_dir: Path, work_root: Path, material_sha256: str | None, probe: Probe) -> int:
    """Clean every record of this material (the caller holds its lock), or every record when `material_sha256`
    is None (the caller holds `convert.lock`, FR-INGEST-1); returns how many were cleaned."""
    if not run_dir.is_dir():
        return 0
    cleaned = 0
    for path in sorted(run_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            continue
        if not isinstance(record, dict):
            continue
        if material_sha256 is not None and record.get("material_sha256") != material_sha256:
            continue
        work = Path(str(record.get("work", "")))
        sid, start = record.get("child_sid"), record.get("child_start")
        if (
            isinstance(sid, int)
            and sid > 1
            and probe.alive(sid)
            and probe.start_time(sid) == start
            and probe.cwd(sid) == os.path.realpath(work)
        ):
            probe.killpg(sid)
        if work.name and _inside(work, work_root):
            shutil.rmtree(work, ignore_errors=True)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        cleaned += 1
    return cleaned


# FR-INGEST-1: the parent's own subprocesses get an allow-listed environment; `ps` and `lsof` need only these
# (LANG=C keeps `ps -o lstart=` in one fixed format).
PROBE_ENV: Final = {"PATH": "/usr/bin:/bin:/usr/sbin", "LANG": "C"}


class SystemProbe:
    """The real process probe: `ps` for the start time, `lsof` for the cwd (no new dependency)."""

    def alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def start_time(self, pid: int) -> str | None:
        done = subprocess.run(  # noqa: S603 (fixed argv, a numeric pid)
            ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
            env=PROBE_ENV,
        )
        value = done.stdout.strip()
        return value or None

    def cwd(self, pid: int) -> str | None:
        argv = ["/usr/sbin/lsof", "-a", "-d", "cwd", "-p", str(pid), "-Fn"]
        done = subprocess.run(  # noqa: S603 (fixed argv)
            argv, capture_output=True, text=True, check=False, env=PROBE_ENV
        )
        for line in done.stdout.splitlines():
            if line.startswith("n"):
                return os.path.realpath(line[1:])
        return None

    def killpg(self, pid: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)
