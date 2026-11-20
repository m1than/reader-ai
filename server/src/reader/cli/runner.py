"""Processes: an allow-listed environment for every child (FR-CLI-11), argv-free errors (FR-CLI-21), and the
watcher spawn with the value hand-off on fd 3 and the readiness byte on fd 4 (FR-CLI-8)."""

import fcntl
import os
import select
import signal
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

ENV_ALLOW: Final = ("PATH", "HOME", "LANG", "DOCKER_HOST", "DOCKER_CONTEXT")
READY: Final = b"1"
BUNDLE_FD: Final = 3
READY_FD: Final = 4
_HIGH_FD: Final = 10  # spawn sources are moved above fds 0-4 so the dup2 actions cannot overwrite each other


def safe_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The only environment any child of `reader` gets: an explicit allow-list, nothing inherited."""
    return {key: environ[key] for key in ENV_ALLOW if key in environ}


@dataclass(frozen=True)
class Result:
    code: int
    out: bytes = b""
    err: bytes = b""


class CommandTimeout(Exception):
    """A child ran past its timeout. Carries no argv: the argv may hold a bootstrap URL (FR-CLI-21)."""


class CommandFailed(Exception):
    """A child could not be started (missing binary, permission). Carries no argv."""


class Stream(Protocol):
    def __iter__(self) -> Iterator[str]: ...
    def close(self) -> None: ...


class Runner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        stdin: bytes | None = None,
        timeout: float | None = None,
    ) -> Result: ...

    def stream(self, argv: Sequence[str], *, env: Mapping[str, str]) -> Stream: ...


class _ProcessStream:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process

    def __iter__(self) -> Iterator[str]:
        out = self._process.stdout
        if out is None:
            return
        for raw in out:
            yield raw.decode("utf-8", "replace")
        self._process.wait()

    def close(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(5)
            except subprocess.TimeoutExpired:
                self._process.kill()


class SubprocessRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        stdin: bytes | None = None,
        timeout: float | None = None,
    ) -> Result:
        if env is None:
            raise TypeError("env is required: children never inherit the environment (FR-CLI-11)")
        failure: type[Exception] | None = None
        done: subprocess.CompletedProcess[bytes] | None = None
        try:
            if stdin is None:
                done = subprocess.run(  # noqa: S603 - argv lists built by reader, never a shell string
                    list(argv),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=timeout,
                    env=dict(env),
                    check=False,
                )
            else:
                done = subprocess.run(  # noqa: S603 - argv lists built by reader, never a shell string
                    list(argv),
                    input=stdin,
                    capture_output=True,
                    timeout=timeout,
                    env=dict(env),
                    check=False,
                )
        except subprocess.TimeoutExpired:
            failure = CommandTimeout
        except OSError:
            failure = CommandFailed
        # Raised outside the except blocks, so no exception that carries the argv is chained as __context__.
        if failure is not None or done is None:
            raise (failure or CommandFailed)()
        return Result(done.returncode, done.stdout, done.stderr)

    def stream(self, argv: Sequence[str], *, env: Mapping[str, str]) -> Stream:
        started: subprocess.Popen[bytes] | None = None
        try:
            started = subprocess.Popen(  # noqa: S603 - argv lists built by reader, never a shell string
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=dict(env),
            )
        except OSError:
            started = None
        if started is None:
            raise CommandFailed()
        return _ProcessStream(started)


class WatcherProcess(Protocol):
    pid: int

    def wait_ready(self, timeout: float) -> bool: ...
    def terminate(self) -> None: ...


class Spawner(Protocol):
    def spawn(self, argv: Sequence[str], *, env: Mapping[str, str], bundle: bytes) -> WatcherProcess: ...


class _Spawned:
    def __init__(self, pid: int, ready_fd: int) -> None:
        self.pid = pid
        self._ready_fd: int | None = ready_fd

    def wait_ready(self, timeout: float) -> bool:
        fd = self._ready_fd
        if fd is None:
            return False
        self._ready_fd = None
        try:
            readable, _, _ = select.select([fd], [], [], timeout)
            return bool(readable) and os.read(fd, 1) == READY
        finally:
            os.close(fd)

    def terminate(self) -> None:
        try:
            os.kill(self.pid, signal.SIGTERM)
            os.waitpid(self.pid, os.WNOHANG)
        except ProcessLookupError, ChildProcessError:
            pass


def _high(fd: int) -> int:
    moved = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, _HIGH_FD)
    os.close(fd)
    return moved


class PosixSpawner:
    """Exec-only start (posix_spawn, new session): no Python code runs between fork and exec, which keeps the
    macOS Keychain client fork-safe (FR-CLI-8). fds 0-2 are /dev/null; fd 3 carries the bundle, fd 4 the
    readiness byte; every other descriptor of `reader` is close-on-exec."""

    def spawn(self, argv: Sequence[str], *, env: Mapping[str, str], bundle: bytes) -> WatcherProcess:
        bundle_r, bundle_w = (_high(fd) for fd in os.pipe())
        ready_r, ready_w = (_high(fd) for fd in os.pipe())
        devnull = _high(os.open(os.devnull, os.O_RDWR))
        actions = [
            (os.POSIX_SPAWN_DUP2, devnull, 0),
            (os.POSIX_SPAWN_DUP2, devnull, 1),
            (os.POSIX_SPAWN_DUP2, devnull, 2),
            (os.POSIX_SPAWN_DUP2, bundle_r, BUNDLE_FD),
            (os.POSIX_SPAWN_DUP2, ready_w, READY_FD),
        ]
        try:
            pid = os.posix_spawn(argv[0], list(argv), dict(env), file_actions=actions, setsid=True)
        finally:
            for fd in (bundle_r, ready_w, devnull):
                os.close(fd)
        try:
            view = memoryview(bundle)
            while view:
                view = view[os.write(bundle_w, view) :]
        except BrokenPipeError:
            pass  # the child is gone; wait_ready sees EOF and reports not ready
        finally:
            os.close(bundle_w)
        return _Spawned(pid, ready_r)
