"""`reader-ingest --watch`: claim uploads from the stack and convert them on the host (FR-INGEST-21).

The watcher holds no secret. It waits without `convert.lock` (`next --peek`), takes the lock only to claim
and convert (one converter run on the host at a time, FR-INGEST-22), fetches the bytes over `docker exec`
stdout with a bound, converts only bytes whose length and sha256 equal the claim's and pass the magic check,
and returns the artifact over receive's stdin with the claim. A 30 s heartbeat renews the lease from the claim
to receive's reply; a cancel reply to any call abandons the run without `fail`. In watch mode no path from the
stack is ever opened, and every exec argv is a list of validated values.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

import reader.ingest as ingest
from reader.ingest import magic, params, tarstream
from reader.ingest.host import records
from reader.ingest.host.cli import ENV_ALLOWED, RECEIVE_TIMEOUT_S, Stop
from reader.ingest.host.execio import BoundedRunner, Exec, reply

log = logging.getLogger("reader.ingest.host")

IDLE_WAIT_S: Final = 10.0
EXEC_TIMEOUT: Final = {"next": 45.0, "fetch": 330.0, "progress": 20.0, "fail": 20.0}
SHA_RE: Final = re.compile(r"[0-9a-f]{64}")
CLAIM_RE: Final = re.compile(r"[0-9a-f]{32}")
RESULTS: Final = ("queued", "already_queued", "already_done", "dead")


class Held(Protocol):
    def release(self) -> None: ...


class Awake(Protocol):
    def stop(self) -> None: ...


class TimerHandle(Protocol):
    def cancel(self) -> None: ...


@dataclass
class WatchDeps:
    runner: BoundedRunner
    data_dir: Path
    container: Callable[[], str]  # FR-INGEST-2's worker resolution; raises Stop("stack_unhealthy")
    setup_state: Callable[[], str]  # ready | setup_missing (FR-INGEST-3 and INV-INGEST-4's run-time probe)
    strays: Callable[[], int]
    try_lock: Callable[[], Held | None]  # convert.lock without blocking
    cleanup: Callable[[], int]  # the stale cleanup of every record (FR-INGEST-22)
    convert: Callable[[Path, Callable[[int, int], None]], dict[str, bytes]]  # raises Stop with its code
    caffeinate: Callable[[], Awake]
    timer: Callable[[float, Callable[[], None]], TimerHandle]
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    watcher_sha: str = ""
    abort: Callable[[], None] = lambda: None  # kill the current run's processes (a cancel reply)
    on_busy: Callable[[str], None] = lambda _: None  # rewrite watch.lock's `busy`


def _env() -> dict[str, str]:
    return {k: os.environ[k] for k in ENV_ALLOWED if k in os.environ}


def _canonical_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def claim_shape(obj: Any) -> bool:
    """`{"upload": null}` or exactly {id, sha256, size, claim} with a UUID, 64 hex, 1..cap and 32 hex."""
    if not isinstance(obj, dict) or set(obj) != {"upload"}:
        return False
    up = obj["upload"]
    if up is None:
        return True
    return (
        isinstance(up, dict)
        and set(up) == {"id", "sha256", "size", "claim"}
        and _canonical_uuid(up["id"])
        and isinstance(up["sha256"], str)
        and SHA_RE.fullmatch(up["sha256"]) is not None
        and type(up["size"]) is int
        and 1 <= up["size"] <= params.UPLOAD_BYTES_MAX
        and isinstance(up["claim"], str)
        and CLAIM_RE.fullmatch(up["claim"]) is not None
    )


def _peek_shape(obj: Any) -> bool:
    if not isinstance(obj, dict) or set(obj) != {"queued"}:
        return False
    return type(obj["queued"]) is int and obj["queued"] >= 0


def _ok_shape(obj: Any) -> bool:
    return bool(obj == {"ok": True})


def _receive_shape(obj: Any) -> bool:
    return (
        isinstance(obj, dict)
        and set(obj) == {"result", "artifact_id"}
        and obj["result"] in RESULTS
        and isinstance(obj["artifact_id"], str)
        and SHA_RE.fullmatch(obj["artifact_id"]) is not None
    )


@dataclass
class Claim:
    id: str
    sha256: str
    size: int
    claim: str


class Cancelled(Exception):
    """A cancel reply: abandon the run without `fail`."""


class Watcher:
    def __init__(self, deps: WatchDeps) -> None:
        self.d = deps
        self.busy = "none"

    # ---- execs ----

    def _inbox(self, container: str, args: list[str], timeout: float, limit: int = params.REPLY_MAX,
               sink: Callable[[bytes], None] | None = None) -> Exec:
        argv = ["docker", "exec", "-i", container, "python", "-I", "-m", "reader.ingest.inbox", *args]
        return self.d.runner.run(argv, _env(), timeout, limit, None, sink)

    def _next(self, container: str, mode: str, wait: int, busy: str, state: str, strays: int) -> Exec:
        args = [
            "next", mode, "--wait", str(wait), "--host-state", state, "--strays", str(strays),
            "--converter-version", ingest.CONVERTER_VERSION, "--watcher-sha", self.d.watcher_sha,
            "--busy", busy,
        ]
        return self._inbox(container, args, EXEC_TIMEOUT["next"])

    # ---- the loop ----

    def start(self) -> None:
        """At start, once: take convert.lock without blocking, clean every stale record, release it."""
        held = self.d.try_lock()
        if held is None:
            return  # its holder is live
        try:
            self.d.cleanup()
        finally:
            held.release()

    def step(self) -> None:
        """One loop step; an OSError (a full disk, a vanished file) never ends the watcher: a held claim is
        failed inside the run, then the loop waits 10 s and continues (code review round 1)."""
        try:
            self._step()
        except OSError as error:
            log.warning("step_error errno=%s wait_s=%d", error.errno, IDLE_WAIT_S)
            self.d.sleep(IDLE_WAIT_S)

    def _step(self) -> None:
        try:
            container = self.d.container()
        except Stop:
            log.info("stack_unhealthy wait_s=%d", IDLE_WAIT_S)
            self.d.sleep(IDLE_WAIT_S)
            return
        state, strays = self.d.setup_state(), self.d.strays()
        if strays > 0:  # a warning only: never killed, claims continue (FR-INGEST-22)
            log.warning("stray_processes count=%d", strays)
        peek = self._next(container, "--peek", params.WAIT_MAX_S, "none", state, strays)
        kind, peeked = reply(peek, _peek_shape)
        if kind != "ok":
            log.info("peek_failed reply=%s", kind)
            self.d.sleep(IDLE_WAIT_S)
            return
        if peeked["queued"] == 0:
            return  # the peek itself waited up to 25 s
        if state != "ready":
            self.d.sleep(IDLE_WAIT_S)
            return
        held = self.d.try_lock()
        if held is None:  # a command-line conversion holds convert.lock
            self._next(container, "--peek", 1, "cli_running", state, strays)
            self.d.sleep(IDLE_WAIT_S)
            return
        claimed: Claim | None = None
        try:
            self.d.cleanup()
            kind, obj = reply(self._next(container, "--claim", 1, "none", state, strays), claim_shape)
            if kind == "ok" and obj["upload"] is not None:
                claimed = Claim(**obj["upload"])
                self._run(container, claimed)
            else:
                log.info("claim_none reply=%s", kind)
        finally:
            held.release()  # after the run's processes are killed and its WORK removed (_run's cleanup)
        if claimed is None:
            self.d.sleep(IDLE_WAIT_S)

    def run_forever(self, stop: threading.Event) -> None:
        self.start()
        while not stop.is_set():
            self.step()

    # ---- one claim ----

    def _set_busy(self, busy: str) -> None:
        self.busy = busy
        self.d.on_busy(busy)

    def _run(self, container: str, c: Claim) -> None:
        log.info("upload_claimed id=%s sha=%s size=%d", c.id, c.sha256[:12], c.size)
        started = self.d.clock()
        self._set_busy("claim")
        awake = self.d.caffeinate()
        phase = ["fetch"]
        cancelled = threading.Event()

        def call(args: list[str], timeout: float, shape: Callable[[Any], bool]) -> tuple[str, Any]:
            kind, obj = reply(self._inbox(container, args, timeout), shape)
            if kind == "cancel":
                cancelled.set()
            return kind, obj

        def beat() -> None:
            args = [*self._claim_args("progress", c), "--phase", phase[0]]
            kind, _ = call(args, EXEC_TIMEOUT["progress"], _ok_shape)
            if kind == "cancel":
                log.info("upload_cancelled id=%s phase=%s", c.id, phase[0])
                self.d.abort()

        heart = self.d.timer(params.HEARTBEAT_S, beat)
        run_dir, work_root = self.d.data_dir / "ingest" / "run", self.d.data_dir / "ingest" / "work"
        name = records.new_run(run_dir, work_root, c.sha256, upload_id=c.id)
        work = work_root / name
        try:
            try:
                code = self._convert_and_send(container, c, work, phase, call, cancelled)
            except OSError as error:  # with the claim held: fail it, never leave it to the lease
                log.warning("run_error id=%s errno=%s", c.id, error.errno)
                code = "convert_failed"
            if code is not None and code not in params.FAIL_CODES:
                log.info("run_code id=%s code=%s reported=convert_failed", c.id, code)
                code = "convert_failed"
            if code is not None and not cancelled.is_set():
                call([*self._claim_args("fail", c), "--code", code], EXEC_TIMEOUT["fail"], _ok_shape)
                log.info("upload_failed id=%s code=%s s=%.1f", c.id, code, self.d.clock() - started)
        finally:
            heart.cancel()
            awake.stop()
            records.end_run(run_dir, work_root, name)
            self._set_busy("none")

    @staticmethod
    def _claim_args(command: str, c: Claim) -> list[str]:
        return [command, "--upload-id", c.id, "--claim", c.claim]

    def _fetch(self, container: str, c: Claim, target: Path) -> str | None:
        """The bytes of this claim into WORK; None when they are exactly the claim's, else a fail code."""
        target.parent.mkdir(mode=0o700, exist_ok=True)
        digest = hashlib.sha256()
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        head, tail = bytearray(), bytearray()

        def sink(chunk: bytes) -> None:
            os.write(fd, chunk)
            digest.update(chunk)
            if len(head) < 8:
                head.extend(chunk[: 8 - len(head)])
            tail.extend(chunk)
            del tail[: max(0, len(tail) - magic.TAIL_BYTES)]

        try:
            result = self._inbox(container, self._claim_args("fetch", c), EXEC_TIMEOUT["fetch"], c.size, sink)
        finally:
            os.close(fd)
        os.chmod(target, 0o400)
        if result.timed_out:
            return "timeout"
        if result.code == 3:
            kind, _ = reply(result, lambda _: False)
            if kind == "cancel":
                raise Cancelled
            return "bad_reply"
        if result.code == 1:
            kind, code = reply(result, lambda _: False)
            return "inbox_missing" if (kind, code) == ("error", "missing") else "fetch_mismatch"
        if result.code != 0 or result.overflow or result.total != c.size or digest.hexdigest() != c.sha256:
            return "fetch_mismatch"
        if not magic.check(bytes(head), bytes(tail)):
            return "not_pdf"
        log.info("fetch_done id=%s bytes=%d stderr_bytes=%d", c.id, result.total, result.stderr_bytes)
        return None

    def _convert_and_send(
        self,
        container: str,
        c: Claim,
        work: Path,
        phase: list[str],
        call: Callable[[list[str], float, Callable[[Any], bool]], tuple[str, Any]],
        cancelled: threading.Event,
    ) -> str | None:
        """None on success or cancel; else the code for `fail`."""
        try:
            problem = self._fetch(container, c, work / "in" / "material.pdf")
            if problem is not None:
                return problem
            if cancelled.is_set():  # a heartbeat's cancel during the fetch: the converter never starts
                return None
            phase[0] = "convert"
            last = [-1e9]

            def on_pages(done: int, total: int) -> None:
                if self.d.clock() - last[0] >= params.HEARTBEAT_S or done == total:
                    last[0] = self.d.clock()
                    pages = ["--pages-done", str(done), "--pages-total", str(total)]
                    call([*self._claim_args("progress", c), *pages], EXEC_TIMEOUT["progress"], _ok_shape)

            try:
                members = self.d.convert(work, on_pages)
            except Stop as stop:
                if cancelled.is_set():
                    return None
                if stop.code == "interrupted":
                    raise
                return stop.code
            if cancelled.is_set():
                return None
            phase[0] = "sending"
            sending = [*self._claim_args("progress", c), "--phase", "sending"]
            kind, _ = call(sending, EXEC_TIMEOUT["progress"], _ok_shape)
            if kind == "cancel":
                return None
            if kind != "ok":
                return {"bad_reply": "bad_reply", "timeout": "timeout"}.get(kind, "receive_failed")
            phase[0] = "receive"
            buffer = io.BytesIO()
            tarstream.write_members(members, buffer)
            argv = ["docker", "exec", "-i", container, "python", "-I", "-m", "reader.ingest.receive",
                    "--upload-id", c.id, "--claim", c.claim]
            tar = buffer.getvalue()
            result = self.d.runner.run(argv, _env(), RECEIVE_TIMEOUT_S, params.REPLY_MAX, tar, None)
            kind, obj = reply(result, _receive_shape)
            if kind == "cancel":
                return None
            if kind == "ok":
                log.info("upload_sent id=%s result=%s", c.id, obj["result"])
                return None
            return {"error": "receive_failed", "timeout": "timeout"}.get(kind, "bad_reply")
        except Cancelled:
            cancelled.set()
            return None


# ---- locks (FR-INGEST-21, -22) ----


@dataclass
class _FdLock:
    fd: int
    released: bool = False

    def release(self) -> None:
        if not self.released:
            self.released = True
            os.close(self.fd)


def try_convert_lock(data_dir: Path) -> _FdLock | None:
    """`convert.lock` without blocking (O_CLOEXEC: no child holds it); None while another run holds it."""
    locks = data_dir / "ingest" / "locks"
    locks.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(locks / "convert.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    if not records.try_flock(fd):
        os.close(fd)
        return None
    return _FdLock(fd)


def clean_all(data_dir: Path, probe: records.Probe) -> int:
    """The stale cleanup that taking convert.lock includes: every run record, CLI or watch (FR-INGEST-22)."""
    root = data_dir / "ingest"
    return records.cleanup_stale(root / "run", root / "work", None, probe)


@dataclass
class WatchLock:
    fd: int
    pid: int
    sha: str

    def set_busy(self, busy: str) -> None:
        """Rewrite the one JSON line in place at every claim and release (OPS-CLI FR-CLI-22 reads it)."""
        line = json.dumps({"pid": self.pid, "sha": self.sha, "busy": busy}).encode()
        os.ftruncate(self.fd, 0)
        os.pwrite(self.fd, line, 0)


@contextlib.contextmanager
def watch_lock(data_dir: Path, pid: int, sha: str) -> Iterator[WatchLock]:
    locks = data_dir / "ingest" / "locks"
    locks.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(locks / "watch.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        if not records.try_flock(fd):
            raise Stop("busy", 3, "an ingest watcher is already running")
        lock = WatchLock(fd, pid, sha)
        lock.set_busy("none")
        yield lock
    finally:
        os.close(fd)


# ---- the Mac stays awake while a claim is held ----


@dataclass
class _Caffeinate:
    process: subprocess.Popen[bytes]

    def stop(self) -> None:
        with contextlib.suppress(ProcessLookupError, OSError):
            self.process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.process.wait(5)


def start_caffeinate(pid: int) -> _Caffeinate:
    """An idle-sleep assertion that also ends if the watcher dies (`-w`)."""
    process = subprocess.Popen(  # noqa: S603 (fixed argv, a numeric pid)
        ["/usr/bin/caffeinate", "-i", "-w", str(pid)],
        env=_env(),
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return _Caffeinate(process)


SETUP_RECHECK_S: Final = 6 * 3600.0  # a full model hash at least this often (chosen)


@dataclass
class SetupCheck:
    """FR-INGEST-3's check for an idle watcher: the full check (every model file hashed, about 1.2 GB) at
    start, then per step only the cheap record comparison and the model files' sizes and mtimes; the full
    check runs again when those change or every SETUP_RECHECK_S (code review round 1)."""

    record: Callable[[], str | None]
    full: Callable[[], str | None]
    fingerprint: Callable[[], object]
    clock: Callable[[], float] = time.monotonic
    interval_s: float = SETUP_RECHECK_S
    _seen: tuple[object, float, str | None] | None = None

    def problem(self) -> str | None:
        cheap = self.record()
        if cheap is not None:
            return cheap
        mark, now = self.fingerprint(), self.clock()
        if self._seen is None or self._seen[0] != mark or now - self._seen[1] >= self.interval_s:
            self._seen = (mark, now, self.full())
        return self._seen[2]


def run_watch(data_dir: Path, deps: Any) -> int:
    """`reader-ingest --watch`: watch.lock (held: exit 3 busy), then the loop until SIGINT or SIGTERM, which
    the CLI's handler raises as Stop("interrupted") inside the current step (its run's processes are killed
    and its WORK removed on the way out; the claim's lease then expires and the upload is queued again)."""
    from reader.ingest.host import cli, crossprobe, execio, setup, watchdog
    from reader.ingest.host.closure import source_sha256

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sha = source_sha256()
    cli.resolve_paths(deps, data_dir)
    venv, pyroot, models = deps.venv, deps.pyroot, deps.models
    if venv is None or pyroot is None or models is None:
        raise Stop("setup_missing", 2)
    probe: list[str] = []
    current: list[Any] = []
    inspect = watchdog.SystemInspect()
    run_dir = data_dir / "ingest" / "run"

    checked = SetupCheck(
        record=lambda: setup.check_record(data_dir),
        full=lambda: deps.check_setup(data_dir),
        fingerprint=lambda: setup.models_fingerprint(data_dir),
    )

    def setup_state() -> str:
        if checked.problem() is not None:
            return "setup_missing"
        if not probe:  # once per watcher start (INV-INGEST-4 at run time)
            probe.append(crossprobe.run(venv, pyroot, models, data_dir))
        return probe[0]

    def convert(work: Path, on_pages: Callable[[int, int], None]) -> dict[str, bytes]:
        def on_start(child: Any) -> None:
            current[:] = [child]
            records.set_child(run_dir, work.name, child.pid, deps.start_time(child.pid) or "")

        try:
            def report(line: str) -> None:
                log.info("convert %s", line)

            members, meta = cli.convert_members(work, deps, report, on_start, on_pages)
        finally:
            current.clear()
        digest = hashlib.sha256()
        with open(work / "in" / "material.pdf", "rb") as handle:  # the copy this watcher fetched and verified
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        if meta["material_sha256"] != digest.hexdigest():
            raise Stop("convert_failed", 5)
        return members

    def abort() -> None:  # FR-INGEST-16: SIGTERM to the child's session, SIGKILL after 10 s
        for child in list(current):
            child.terminate()
            threading.Timer(cli.CHILD_GRACE_S, child.kill).start()

    venv_python = str(venv / "bin" / "python")
    with watch_lock(data_dir, os.getpid(), sha) as lock:
        wdeps = WatchDeps(
            runner=execio.SystemRunner(),
            data_dir=data_dir,
            container=lambda: cli.worker_container(deps),
            setup_state=setup_state,
            strays=lambda: watchdog.strays(inspect.snapshot(), venv_python, set()),
            try_lock=lambda: try_convert_lock(data_dir),
            cleanup=lambda: clean_all(data_dir, deps.probe),
            convert=convert,
            caffeinate=lambda: start_caffeinate(os.getpid()),
            timer=lambda interval, fn: RepeatTimer(interval, fn),
            watcher_sha=sha,
            abort=abort,
            on_busy=lock.set_busy,
        )
        log.info("watch_start sha=%s converter=%s", sha[:12], ingest.CONVERTER_VERSION)
        Watcher(wdeps).run_forever(threading.Event())
    return 0


@dataclass
class RepeatTimer:
    """Runs `fn` every `interval` seconds on its own thread until cancelled (the heartbeat)."""

    interval: float
    fn: Callable[[], None]
    _stop: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            with contextlib.suppress(Exception):
                self.fn()

    def cancel(self) -> None:
        self._stop.set()
