"""Host watchdogs over a converter run's processes (SRV-INGEST FR-INGEST-22).

sandbox-exec confines what a process may touch, not what it may consume, so the parent counts the run's
processes every tick and kills them all over `convert.rss_max` (memory) or `convert.work_bytes_max` (work
directory). A process belongs to this run when its session id is the child's, or when it runs the converter's
Python (kernel executable path), started at or after the child, is not the watcher or one of its ancestors,
and is bound to this run's WORK by its cwd or its args (a descendant that called `setsid()`). Each member is
re-verified just before its SIGKILL.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import re
import signal
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final, Protocol

from reader.ingest import params

log = logging.getLogger("reader.ingest.host")

PS_ENV: Final = {"PATH": "/usr/bin:/bin:/usr/sbin", "LANG": "C"}
OVER: Final = 1 << 62  # a work directory past the entry bound counts as over every limit
_LSTART: Final = r"\w{3}\s+\w{3}\s+\d+\s+\d\d:\d\d:\d\d\s+\d{4}"
_PS_LINE: Final = re.compile(rf"\s*(\d+)\s+(\d+)\s+({_LSTART})\s+(\d+)\s(.*)")


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    start: str  # `ps -o lstart=` (LANG=C)
    rss_kb: int
    comm: str


@dataclass(frozen=True)
class Run:
    child_sid: int
    child_start: str
    work: str  # realpath
    exclude: frozenset[int] = field(default_factory=frozenset)


class Inspect(Protocol):
    def snapshot(self) -> list[Proc]: ...
    def sid(self, pid: int) -> int | None: ...
    def exe(self, pid: int) -> str | None: ...
    def cwd(self, pid: int) -> str | None: ...
    def args(self, pid: int) -> str | None: ...
    def start(self, pid: int) -> str | None: ...
    def kill(self, pid: int) -> None: ...


def parse_ps(out: str) -> list[Proc]:
    procs = []
    for line in out.splitlines():
        m = _PS_LINE.fullmatch(line.rstrip("\n"))
        if m:
            procs.append(Proc(int(m[1]), int(m[2]), " ".join(m[3].split()), int(m[4]), m[5].strip()))
    return procs


def _when(start: str | None) -> datetime | None:
    if not start:
        return None
    try:
        return datetime.strptime(" ".join(start.split()), "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return None


def _bound(run: Run, cwd: str | None, args: str | None) -> bool:
    inside = cwd is not None and (cwd == run.work or cwd.startswith(run.work + "/"))
    return inside or (args is not None and (run.work + "/") in args)


def _member(run: Run, proc: Proc, inspect: Inspect, converter_exe: str) -> bool:
    if proc.pid in run.exclude:
        return False
    when, first = _when(proc.start), _when(run.child_start)
    if when is None or first is None or when < first:
        return False
    if inspect.sid(proc.pid) == run.child_sid:
        return True
    if inspect.exe(proc.pid) != converter_exe:
        return False
    return _bound(run, inspect.cwd(proc.pid), inspect.args(proc.pid))


def members(run: Run, procs: Iterable[Proc], inspect: Inspect, converter_exe: str) -> dict[int, Proc]:
    return {p.pid: p for p in procs if _member(run, p, inspect, converter_exe)}


def strays(procs: Iterable[Proc], venv_python: str, live: set[int]) -> int:
    """Detached processes whose `comm` is exactly the converter venv's python and that belong to no live run.
    A warning only: never killed (unverified)."""
    return sum(1 for p in procs if p.comm == venv_python and p.ppid == 1 and p.pid not in live)


def work_bytes(work: Path, max_entries: int = params.WORK_ENTRIES_MAX) -> int:
    total = entries = 0
    stack = [str(work)]
    while stack:
        try:
            scan = os.scandir(stack.pop())
        except OSError:
            continue
        with scan:
            for entry in scan:
                entries += 1
                if entries > max_entries:
                    return OVER
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    with contextlib.suppress(OSError):
                        total += entry.stat(follow_symlinks=False).st_size
    return total


class WatchdogBlind(Exception):
    """A tick whose member set lacks the child while the child lives (FR-INGEST-22, fail closed)."""


class Watchdog:
    def __init__(
        self,
        run: Run,
        inspect: Inspect,
        converter_exe: str,
        *,
        rss_max: int,
        work_max: int,
        work: Path,
        alive: Callable[[], bool] | None = None,
    ) -> None:
        """`alive`: whether the child lives, read from the child process itself (never from `ps`, which may
        print nothing: code review round 2); without it, the child's start time is asked."""
        self.run, self.inspect, self.converter_exe = run, inspect, converter_exe
        self.rss_max, self.work_max, self.work = rss_max, work_max, work
        self.alive = alive
        self.peak_rss = 0

    def _snapshot(self) -> tuple[dict[int, Proc], dict[int, str | None]]:
        found = members(self.run, self.inspect.snapshot(), self.inspect, self.converter_exe)
        return found, {pid: self.inspect.exe(pid) for pid in found}

    def _child_alive(self) -> bool:
        if self.alive is not None:
            return self.alive()
        start = self.inspect.start(self.run.child_sid)
        return start is not None and " ".join(start.split()) == " ".join(self.run.child_start.split())

    def check(self) -> str | None:
        """A limit's code, or None. Raises WatchdogBlind when the snapshot misses the live child (an empty or
        truncated `ps`): the caller counts that tick as failed (fail closed)."""
        started = time.monotonic()
        found, exes = self._snapshot()
        if self.run.child_sid not in found and self._child_alive():
            raise WatchdogBlind
        rss = sum(p.rss_kb for p in found.values()) * 1024
        self.peak_rss = max(self.peak_rss, rss)
        code = None
        if rss > self.rss_max:
            code = "memory_limit"
        elif work_bytes(self.work) > self.work_max:
            code = "work_limit"
        if code is not None:
            self._kill(found, exes)
        elapsed = time.monotonic() - started
        log.info(
            "watchdog_tick ms=%d members=%d rss=%d over=%s", elapsed * 1000, len(found), rss, code or "-"
        )
        if elapsed > params.TICK_BUDGET_S:
            log.warning("watchdog_tick_overrun ms=%d", elapsed * 1000)
        return code

    def kill_all(self) -> None:
        self._kill(*self._snapshot())

    def _kill(self, found: dict[int, Proc], exes: dict[int, str | None]) -> None:
        for pid, proc in found.items():
            start = self.inspect.start(pid)
            still = Proc(pid, proc.ppid, start or "", proc.rss_kb, proc.comm)
            same = start is not None and " ".join(start.split()) == " ".join(proc.start.split())
            if not same or self.inspect.exe(pid) != exes.get(pid):
                continue
            if _member(self.run, still, self.inspect, self.converter_exe):
                self.inspect.kill(pid)


# ---- the real inspection (macOS libproc and sysctl; the unconfined parent may read both) ----

_PROC_PIDVNODEPATHINFO: Final = 9
_VNODE_INFO_SIZE: Final = 152
_MAXPATHLEN: Final = 1024
_CTL_KERN: Final = 1
_KERN_PROCARGS2: Final = 49


class SystemInspect:
    def __init__(self) -> None:
        self._libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)

    def snapshot(self) -> list[Proc]:
        done = subprocess.run(
            ["/bin/ps", "-A", "-o", "pid=,ppid=,lstart=,rss=,comm="],
            capture_output=True, text=True, check=False, env=PS_ENV,
        )
        procs = parse_ps(done.stdout)
        if done.returncode != 0 or not procs:  # `ps -A` always lists this process: nothing is a failed tick
            raise OSError(f"ps exited {done.returncode} with {len(procs)} processes")
        return procs

    def sid(self, pid: int) -> int | None:
        try:
            return os.getsid(pid)
        except OSError:
            return None

    def exe(self, pid: int) -> str | None:
        buf = ctypes.create_string_buffer(4 * _MAXPATHLEN)
        n = self._libc.proc_pidpath(pid, buf, ctypes.c_uint32(len(buf)))
        return os.fsdecode(buf.value) if n > 0 else None

    def cwd(self, pid: int) -> str | None:
        size = 2 * (_VNODE_INFO_SIZE + _MAXPATHLEN)
        buf = ctypes.create_string_buffer(size)
        n = self._libc.proc_pidinfo(pid, _PROC_PIDVNODEPATHINFO, ctypes.c_uint64(0), buf, size)
        if n <= 0:
            return None
        raw = buf.raw[_VNODE_INFO_SIZE : _VNODE_INFO_SIZE + _MAXPATHLEN].split(b"\0", 1)[0]
        return os.path.realpath(os.fsdecode(raw)) if raw else None

    def args(self, pid: int) -> str | None:
        mib = (ctypes.c_int * 3)(_CTL_KERN, _KERN_PROCARGS2, pid)
        size = ctypes.c_size_t(0)
        if self._libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or size.value == 0:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if self._libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
            return None
        data = buf.raw[: size.value]
        argc = int.from_bytes(data[:4], "little")
        parts = [p for p in data[4:].split(b"\0") if p]
        return " ".join(os.fsdecode(p) for p in parts[1 : argc + 1])  # parts[0] is the exec path

    def start(self, pid: int) -> str | None:
        done = subprocess.run(  # noqa: S603 (fixed argv, a numeric pid)
            ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, check=False, env=PS_ENV,
        )
        return done.stdout.strip() or None

    def kill(self, pid: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)


def ancestors(pid: int, procs: Iterable[Proc]) -> frozenset[int]:
    """`pid` and its ancestors (the watcher or CLI parent never counts as a member)."""
    parent = {p.pid: p.ppid for p in procs}
    seen = {pid}
    while (pid := parent.get(pid, 0)) > 1 and pid not in seen:
        seen.add(pid)
    return frozenset(seen | {1})
