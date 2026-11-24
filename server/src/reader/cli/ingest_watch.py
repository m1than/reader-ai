"""The ingest watcher under `reader up`, `reader status` and `reader down` (OPS-CLI FR-CLI-23).

SRV-INGEST FR-INGEST-21's watcher. Unlike the secret watcher (FR-CLI-8), the spawn passes nothing: an
allow-listed environment, only fds 0, 1 and 2, stdin `/dev/null`, its own session and no terminal. Its lock
`<data_dir>/ingest/locks/watch.lock` holds one JSON line `{pid, sha, busy}`; a line that does not parse counts
as busy.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import signal
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from reader.config import ReaderConfig

if TYPE_CHECKING:
    from reader.cli.commands import Ctx

ALLOWED: Final = ("PATH", "HOME", "LANG", "DOCKER_HOST")  # SRV-INGEST FR-INGEST-2's allow-list
LOG_NAME: Final = "ingest-watch.log"
LOG_KEEP: Final = 5 << 20  # the log is truncated to its last 5 MiB at each start
STOP_GRACE_S: Final = 15.0
POLL_S: Final = 0.5
STALE: Final = "ingest watcher runs older code; it restarts at the next `reader up` when idle"
_SPAWNED: list[subprocess.Popen[bytes]] = []  # kept, so the interpreter never reaps a running watcher


@dataclass(frozen=True)
class Holder:
    pid: int | None
    sha: str | None
    busy: str | None  # None: the content does not parse, which counts as busy


def data_dir_of(config: ReaderConfig, config_path: Path) -> Path:
    data_dir = Path(config.instance.data_dir)
    return data_dir if data_dir.is_absolute() else config_path.resolve().parent / data_dir


def _locks(data_dir: Path) -> Path:
    return data_dir / "ingest" / "locks"


def _held(path: Path) -> bytes | None:
    """The content of a held lock file, or None when nobody holds it (FR-CLI-7's probe)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return os.pread(fd, 4096, 0)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


def holder(data_dir: Path) -> Holder | None:
    raw = _held(_locks(data_dir) / "watch.lock")
    if raw is None:
        return None
    try:
        obj = json.loads(raw)
    except ValueError:
        return Holder(None, None, None)
    if not isinstance(obj, dict) or set(obj) != {"pid", "sha", "busy"} or type(obj["pid"]) is not int:
        return Holder(None, None, None)
    if not isinstance(obj["sha"], str) or obj["busy"] not in ("none", "claim", "cli_running"):
        return Holder(None, None, None)
    return Holder(obj["pid"], obj["sha"], obj["busy"])


def checkout_sha() -> str:
    """The `converter_source_sha256` of this checkout's code (SRV-INGEST FR-INGEST-3)."""
    from reader.ingest.host.closure import source_sha256

    return source_sha256()


def state(data_dir: Path, sha: str) -> str:
    """`running` | `stale` (other or unknown code) | `stopped`."""
    found = holder(data_dir)
    if found is None:
        return "stopped"
    return "running" if found.sha == sha else "stale"


def converter(data_dir: Path) -> str:
    """`running` while a conversion holds convert.lock (one run on the host at a time), else `idle`."""
    return "running" if _held(_locks(data_dir) / "convert.lock") is not None else "idle"


def watcher_argv(config_path: Path) -> list[str]:
    return [str(Path(sys.executable).with_name("reader-ingest")), "--watch", "--config", str(config_path)]


def _trim(log_path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        if log_path.stat().st_size > LOG_KEEP:
            fd = os.open(log_path, os.O_RDWR | os.O_NOFOLLOW)
            try:
                size = os.fstat(fd).st_size
                tail = os.pread(fd, LOG_KEEP, size - LOG_KEEP)
                os.ftruncate(fd, 0)
                os.pwrite(fd, tail, 0)
            finally:
                os.close(fd)


def spawn(argv: list[str], env: Mapping[str, str], log_path: Path) -> int:
    """Start the watcher detached: its own session, only fds 0, 1, 2, stdin /dev/null, output to the log."""
    _trim(log_path)
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        process = subprocess.Popen(  # noqa: S603 (a fixed argv list)
            argv,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=fd,
            stderr=fd,
            close_fds=True,
            pass_fds=(),
            start_new_session=True,
        )
    finally:
        os.close(fd)
    _SPAWNED.append(process)
    return process.pid


def stop(ctx: Ctx, data_dir: Path) -> bool:
    """SIGTERM to the lock file's pid, only while the lock is held; up to 15 s for its release; SIGKILL."""
    found = holder(data_dir)
    if found is None or found.pid is None or found.pid <= 1:
        return False
    with contextlib.suppress(ProcessLookupError, PermissionError):
        ctx.kill(found.pid, signal.SIGTERM)
    deadline = ctx.monotonic() + STOP_GRACE_S
    while holder(data_dir) is not None and ctx.monotonic() < deadline:
        ctx.sleep(POLL_S)
    if holder(data_dir) is not None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            ctx.kill(found.pid, signal.SIGKILL)
    return True


def ensure(ctx: Ctx, config_path: Path, config: ReaderConfig) -> None:
    """`reader up`, once the stack is healthy: start the watcher unless one runs; restart an idle one of other
    code, leave a busy one (reported as stale)."""
    data_dir = data_dir_of(config, config_path)
    found = holder(data_dir)
    if found is not None:
        if found.sha == checkout_sha():
            return
        if found.busy != "none":
            print(STALE, file=ctx.out)
            return
        stop(ctx, data_dir)
    env = {k: v for k, v in ctx.env.items() if k in ALLOWED}
    starter = ctx.ingest_spawn  # `default_ctx` sets `spawn`; a context without one starts no watcher
    if starter is None:
        return
    try:
        starter(watcher_argv(config_path), env, ctx.run_dir / LOG_NAME)
    except OSError:
        print("error: the ingest watcher could not be started; uploads wait as queued", file=ctx.err)
