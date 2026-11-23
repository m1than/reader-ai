"""Host watch mode with a fake stack, a fake clock and fake timers (SRV-INGEST [unit, fake runner]).

@trace FR-INGEST-21 FR-INGEST-22 INV-INGEST-10 FR-INGEST-24 INV-INGEST-4
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import pytest

from .conftest import build_artifact, need

UPLOAD = "6f1c2a3b-4d5e-4f60-8a7b-9c0d1e2f3a4b"
CLAIM = "0123456789abcdef0123456789abcdef"
CANCEL = b'{"cancel":true}\n'


def pdf(tag: bytes = b"") -> bytes:
    return b"%PDF-1.7\n" + tag + b"x" * 100 + b"\n%%EOF\n"


BODY = pdf(b"watch")


def claim_line(body: bytes = BODY, **over: Any) -> bytes:
    digest = hashlib.sha256(body).hexdigest()
    upload = {"id": UPLOAD, "sha256": digest, "size": len(body), "claim": CLAIM, **over}
    return json.dumps({"upload": upload}).encode() + b"\n"


@dataclass
class Clock:
    now: float = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class FakeTimer:
    interval: float
    fn: Callable[[], None]
    cancelled: bool = False

    def cancel(self) -> None:
        self.cancelled = True


@dataclass
class FakeStack:
    """The worker side of `docker exec`: scripted replies per inbox command; records every exec."""

    clock: Clock
    queued: int = 1
    claim_reply: bytes = field(default_factory=claim_line)
    fetch_body: bytes = BODY
    fetch_code: int = 0
    fetch_hang: bool = False
    progress_reply: tuple[int, bytes] = (0, b'{"ok":true}\n')
    receive_reply: tuple[int, bytes] = (0, b'{"artifact_id":"' + b"a" * 64 + b'","result":"queued"}\n')
    stderr: int = 0
    calls: list[list[str]] = field(default_factory=list)
    timeouts: dict[str, float] = field(default_factory=dict)
    stdin: dict[str, bytes] = field(default_factory=dict)
    on_fetch: Callable[[], None] | None = None  # runs while the fetch is in flight (a heartbeat may fire)
    raise_on: str = ""  # an inbox command whose exec raises OSError

    def run(
        self,
        argv: list[str],
        env: dict[str, str],
        timeout: float,
        limit: int,
        stdin: bytes | None = None,
        sink: Callable[[bytes], None] | None = None,
    ) -> Any:
        execio = need("reader.ingest.host.execio")
        assert argv[:4] == ["docker", "exec", "-i", "cid"] and argv[4:7] == ["python", "-I", "-m"]
        assert set(env) <= {"PATH", "HOME", "LANG", "DOCKER_HOST"}
        self.calls.append(argv)
        module, command = argv[7], argv[8] if len(argv) > 8 else ""
        self.timeouts[command if module == "reader.ingest.inbox" else module] = timeout
        if stdin is not None:
            self.stdin[module] = stdin
        if command and command == self.raise_on:
            raise OSError(28, "No space left on device")
        if command == "fetch" and self.on_fetch is not None:
            self.on_fetch()
        if module == "reader.ingest.receive":
            code, out = self.receive_reply
        elif command == "next" and "--peek" in argv:
            if self.queued == 0:
                self.clock.sleep(float(argv[argv.index("--wait") + 1]))
            code, out = 0, json.dumps({"queued": self.queued}).encode() + b"\n"
        elif command == "next":
            code, out = 0, self.claim_reply
        elif command == "fetch":
            if self.fetch_hang:
                return execio.Exec(
                    code=None, out=b"", total=0, stderr_bytes=0, overflow=False, timed_out=True
                )
            code, out = self.fetch_code, self.fetch_body
        elif command == "progress":
            code, out = self.progress_reply
        elif command == "fail":
            code, out = 0, b'{"ok":true}\n'
        else:
            raise AssertionError(argv)
        overflow = len(out) > limit
        out = out[: limit + 1] if overflow else out
        shape = {
            "code": None if overflow else code,
            "total": len(out),
            "stderr_bytes": self.stderr,
            "overflow": overflow,
            "timed_out": False,
        }
        # mirrors the real runner: a sink receives every byte read, and `out` keeps the first 4,097 (round 1)
        if sink is not None:
            sink(out)
        return execio.Exec(out=out[: execio.REPLY_LIMIT + 1], **shape)

    def commands(self) -> list[str]:
        return [" ".join(a[8:10]) if a[7] == "reader.ingest.inbox" else a[7] for a in self.calls]

    def failed(self) -> list[str]:
        return [a[a.index("--code") + 1] for a in self.calls if len(a) > 8 and a[8] == "fail"]


@dataclass
class Harness:
    tmp: Path
    clock: Clock = field(default_factory=Clock)
    setup_state: str = "ready"
    lock_free: bool = True
    events: list[str] = field(default_factory=list)
    timers: list[FakeTimer] = field(default_factory=list)
    converted: list[bytes] = field(default_factory=list)
    convert_hook: Callable[[Path], None] | None = None
    stack: FakeStack = field(init=False)

    def __post_init__(self) -> None:
        self.stack = FakeStack(self.clock)
        self.data = self.tmp / "data"
        self.data.mkdir()

    def lock(self) -> Any:
        if not self.lock_free:
            return None
        self.events.append("lock")
        harness = self

        class Held:
            def release(self) -> None:
                harness.events.append("unlock")

        return Held()

    def convert(self, work: Path, on_pages: Callable[[int, int], None]) -> dict[str, bytes]:
        """WatchDeps.convert: FR-INGEST-4..10 with FR-INGEST-22's watchdogs on `<work>`; raises cli.Stop."""
        self.events.append("convert")
        material = work / "in" / "material.pdf"
        self.converted.append(material.read_bytes())
        assert oct(material.stat().st_mode & 0o777) == "0o400"
        if self.convert_hook is not None:
            self.convert_hook(work)
        on_pages(50, 100)
        return build_artifact(material_sha256=hashlib.sha256(material.read_bytes()).hexdigest())

    def caffeinate(self) -> Any:
        harness = self
        harness.events.append("caffeinate")

        class Awake:
            def stop(self) -> None:
                harness.events.append("awake-stop")

        return Awake()

    def timer(self, interval: float, fn: Callable[[], None]) -> FakeTimer:
        made = FakeTimer(interval, fn)
        self.timers.append(made)
        return made

    def deps(self) -> Any:
        watch = need("reader.ingest.host.watch")
        return watch.WatchDeps(
            runner=self.stack,
            data_dir=self.data,
            container=lambda: "cid",
            setup_state=lambda: self.setup_state,
            strays=lambda: 0,
            try_lock=self.lock,
            cleanup=lambda: self.events.append("cleanup") or 0,
            convert=self.convert,
            caffeinate=self.caffeinate,
            timer=self.timer,
            clock=self.clock.time,
            sleep=self.clock.sleep,
            watcher_sha="c" * 64,
            abort=lambda: self.events.append("abort"),
        )

    def watcher(self) -> Any:
        return need("reader.ingest.host.watch").Watcher(self.deps())


def work_left(h: Harness) -> list[str]:
    root = h.data / "ingest"
    if not root.exists():
        return []
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.parent.name in ("work", "run"))


# ---- FR-INGEST-21: the loop ----


def test_start_takes_cleans_and_releases_the_lock_once(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.stack.queued = 0
    w = h.watcher()
    w.start()
    assert h.events == ["lock", "cleanup", "unlock"]
    w.step()
    assert h.events == ["lock", "cleanup", "unlock"]  # an idle watcher waits without the lock
    assert h.stack.commands() == ["next --peek"]
    peek = h.stack.calls[0]
    assert peek[peek.index("--wait") + 1] == "25" and peek[peek.index("--busy") + 1] == "none"


def test_setup_missing_never_claims_and_never_spins(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.setup_state = "setup_missing"
    w = h.watcher()
    for _ in range(6):  # a fixed number of steps: a step that never waits fails the clock check, not hangs
        w.step()
    assert h.clock.now >= 6 * 10  # each step waited at least 10 s
    assert len(h.stack.calls) <= 6
    assert all("--peek" in a for a in h.stack.calls)
    assert "lock" not in h.events
    assert all(a[a.index("--host-state") + 1] == "setup_missing" for a in h.stack.calls)


def test_lock_held_by_a_cli_run(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.lock_free = False
    w = h.watcher()
    before = h.clock.now
    w.step()
    assert h.stack.commands() == ["next --peek", "next --peek"]
    second = h.stack.calls[1]
    assert second[second.index("--busy") + 1] == "cli_running" and second[second.index("--wait") + 1] == "1"
    assert h.clock.now - before >= 10
    assert not any("--claim" in a for a in h.stack.calls)


def test_null_claim_releases_the_lock_and_waits(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.stack.claim_reply = b'{"upload":null}\n'
    w = h.watcher()
    at = h.clock.now
    w.step()
    assert h.events == ["lock", "cleanup", "unlock"]
    assert h.stack.commands() == ["next --peek", "next --claim"]
    claim = h.stack.calls[1]
    assert claim[claim.index("--wait") + 1] == "1" and claim[claim.index("--busy") + 1] == "none"
    assert h.clock.now - at >= 10  # no exec for 10 s after a null claim


def test_a_claim_converts_sends_and_cleans_up(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    w = h.watcher()
    w.step()
    assert h.stack.commands() == [
        "next --peek", "next --claim", "fetch --upload-id", "progress --upload-id", "progress --upload-id",
        "reader.ingest.receive",
    ]
    progress = [a for a in h.stack.calls if a[8:9] == ["progress"]]
    phases = [a[a.index("--phase") + 1] if "--phase" in a else "pages" for a in progress]
    assert phases == ["pages", "sending"]
    pages = next(a for a in h.stack.calls if "--pages-done" in a)
    assert pages[pages.index("--pages-done") + 1 :] == ["50", "--pages-total", "100"]
    receive = h.stack.calls[-1]
    assert receive[8:] == ["--upload-id", UPLOAD, "--claim", CLAIM]
    assert h.converted == [BODY]
    assert h.events == ["lock", "cleanup", "caffeinate", "convert", "awake-stop", "unlock"]
    assert work_left(h) == []
    assert h.stack.failed() == []
    assert h.timers and all(t.interval == 30 and t.cancelled for t in h.timers)
    assert h.stack.timeouts == {"next": 45, "progress": 20, "fetch": 330, "reader.ingest.receive": 330}


def test_the_run_record_names_the_upload(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    seen: list[dict[str, Any]] = []

    def record(work: Path) -> None:
        path = h.data / "ingest" / "run" / f"{work.name}.json"
        seen.append(json.loads(path.read_text()))
        assert oct(path.stat().st_mode & 0o777) == "0o600"

    h.convert_hook = record
    h.watcher().step()
    assert seen == [
        {"material_sha256": hashlib.sha256(BODY).hexdigest(), "work": seen[0]["work"], "upload_id": UPLOAD}
    ]


def test_heartbeat_reports_the_current_phase(tmp_path: Path) -> None:
    h = Harness(tmp_path)

    def beat(_: Path) -> None:
        h.timers[-1].fn()

    h.convert_hook = beat
    h.watcher().step()
    beats = [a for a in h.stack.calls if a[8:9] == ["progress"] and "--phase" in a]
    assert ["convert" in a for a in beats].count(True) >= 1


def test_watch_lock_content_and_busy(tmp_path: Path) -> None:
    watch = need("reader.ingest.host.watch")
    cli = need("reader.ingest.host.cli")
    with watch.watch_lock(tmp_path, 4242, "c" * 64) as lock:
        path = tmp_path / "ingest" / "locks" / "watch.lock"
        assert json.loads(path.read_text()) == {"pid": 4242, "sha": "c" * 64, "busy": "none"}
        lock.set_busy("claim")
        assert json.loads(path.read_text()) == {"pid": 4242, "sha": "c" * 64, "busy": "claim"}
        with pytest.raises(cli.Stop) as held, watch.watch_lock(tmp_path, 1, "c" * 64):
            pass
        assert (held.value.code, held.value.exit_code) == ("busy", 3)
    with watch.watch_lock(tmp_path, 7, "c" * 64):
        pass


def test_unhealthy_stack_waits_and_retries(tmp_path: Path) -> None:
    cli = need("reader.ingest.host.cli")
    h = Harness(tmp_path)
    deps = h.deps()
    attempts: list[float] = []

    def unhealthy() -> str:
        attempts.append(h.clock.now)
        raise cli.Stop("stack_unhealthy", 3)

    deps.container = unhealthy
    w = need("reader.ingest.host.watch").Watcher(deps)
    for _ in range(3):
        w.step()
    assert len(attempts) == 3 and attempts[1] - attempts[0] >= 10 and h.stack.calls == []


# ---- INV-INGEST-10: confused deputy and framing ----


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"fetch_body": pdf(b"other")}, "fetch_mismatch"),
        ({"fetch_body": BODY + b"!"}, "fetch_mismatch"),  # killed at size + 1
        ({"fetch_body": BODY[:-1]}, "fetch_mismatch"),
        ({"fetch_code": 1, "fetch_body": BODY[:40]}, "fetch_mismatch"),  # exit 1 after a partial body
        ({"fetch_code": 1, "fetch_body": b'{"error":"missing"}\n'}, "inbox_missing"),
        ({"fetch_hang": True}, "timeout"),
    ],
)
def test_fetch_refusals_convert_nothing(tmp_path: Path, change: dict[str, Any], code: str) -> None:
    h = Harness(tmp_path)
    for key, value in change.items():
        setattr(h.stack, key, value)
    h.watcher().step()
    assert "convert" not in h.events
    assert h.stack.failed() == [code]
    assert work_left(h) == [] and h.events[-1] == "unlock"


def test_fetch_that_is_not_a_pdf(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    junk = b"PK\x03\x04" + b"y" * 120
    h.stack.claim_reply = claim_line(junk)
    h.stack.fetch_body = junk
    h.watcher().step()
    assert "convert" not in h.events and h.stack.failed() == ["not_pdf"]


def test_fetch_cancelled_between_next_and_fetch(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.stack.fetch_code, h.stack.fetch_body = 3, CANCEL
    h.watcher().step()
    assert "convert" not in h.events and h.stack.failed() == [] and work_left(h) == []


def test_stderr_is_counted_never_stored(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    h = Harness(tmp_path)
    h.stack.stderr = 1 << 20
    h.watcher().step()
    assert "convert" in h.events and h.stack.failed() == []


@pytest.mark.parametrize(
    "reply",
    [
        claim_line(extra=1),
        claim_line(sha256="/etc/passwd"),
        claim_line(sha256="a" * 63),
        claim_line(size=-1),
        claim_line(size=209715201),
        claim_line(claim="g" * 32),
        claim_line(id="../x"),
        b"x" * 4097,
        claim_line() + claim_line(),
        b"not json\n",
    ],
)
def test_bad_claim_replies(tmp_path: Path, reply: bytes) -> None:
    h = Harness(tmp_path)
    h.stack.claim_reply = reply
    w = h.watcher()
    at = h.clock.now
    w.step()
    assert "convert" not in h.events and "caffeinate" not in h.events
    assert h.events[-1] == "unlock" and h.clock.now - at >= 10
    assert not any(a[8:9] == ["fetch"] for a in h.stack.calls)


@pytest.mark.parametrize(
    ("code", "raw"),
    [
        (0, CANCEL),
        (3, b'{"ok":true}\n'),
        (1, b"\n"),
        (2, b'{"ok":true}\n'),
        (0, b'{"ok":true}\n{"ok":true}\n'),
    ],
)
def test_reply_framing(code: int, raw: bytes) -> None:
    execio = need("reader.ingest.host.execio")
    result = execio.Exec(code=code, out=raw, total=len(raw), stderr_bytes=0, overflow=False, timed_out=False)
    assert execio.reply(result, lambda obj: obj == {"ok": True}) == ("bad_reply", None)


def test_reply_shapes() -> None:
    execio = need("reader.ingest.host.execio")

    def made(code: int | None, raw: bytes, **kw: Any) -> Any:
        base = {"code": code, "out": raw, "total": len(raw), "stderr_bytes": 0}
        base |= {"overflow": False, "timed_out": False}
        return execio.Exec(**{**base, **kw})

    ok = lambda obj: obj == {"ok": True}  # noqa: E731
    assert execio.reply(made(0, b'{"ok":true}\n'), ok) == ("ok", {"ok": True})
    assert execio.reply(made(3, CANCEL), ok) == ("cancel", None)
    assert execio.reply(made(1, b'{"error":"missing"}\n'), ok) == ("error", "missing")
    assert execio.reply(made(None, b"", timed_out=True), ok) == ("timeout", None)
    assert execio.reply(made(None, b"x" * 4097, overflow=True), ok) == ("bad_reply", None)


def test_bounded_runner_kills_at_the_bound(tmp_path: Path) -> None:
    execio = need("reader.ingest.host.execio")
    runner = execio.SystemRunner()
    flood = ["/bin/sh", "-c", "yes x; sleep 30"]
    result = runner.run(flood, {"PATH": "/usr/bin:/bin"}, 20, 4096)
    assert result.overflow and result.code is None and len(result.out) == 4097
    noisy = ["/bin/sh", "-c", "head -c 1048576 /dev/zero >&2; printf ok"]
    result = runner.run(noisy, {"PATH": "/usr/bin:/bin"}, 20, 4096)
    assert (result.code, result.out, result.stderr_bytes) == (0, b"ok", 1 << 20)
    slow = ["/bin/sh", "-c", "sleep 30"]
    result = runner.run(slow, {"PATH": "/usr/bin:/bin"}, 0.5, 4096)
    assert result.timed_out and result.code is None
    got = bytearray()
    result = runner.run(["/bin/sh", "-c", "printf abcdef"], {"PATH": "/usr/bin:/bin"}, 20, 4, sink=got.extend)
    assert result.overflow and bytes(got) == b"abcde"


def test_cancel_during_conversion_kills_the_run(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    cli = need("reader.ingest.host.cli")
    h.stack.progress_reply = (0, b'{"ok":true}\n')

    def cancelled_mid_run(_: Path) -> None:
        h.stack.progress_reply = (3, CANCEL)
        h.timers[-1].fn()  # the heartbeat gets the cancel shape and kills the run
        raise cli.Stop("cancelled", 5)

    h.convert_hook = cancelled_mid_run
    h.watcher().step()
    assert h.stack.failed() == [] and work_left(h) == []
    assert "reader.ingest.receive" not in h.stack.commands()
    assert h.events[-2:] == ["awake-stop", "unlock"]


def test_cancel_during_receive(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.stack.receive_reply = (3, CANCEL)
    h.watcher().step()
    assert h.stack.failed() == [] and work_left(h) == [] and h.events[-1] == "unlock"


@pytest.mark.parametrize(
    ("reply", "code"),
    [((1, b'{"error":"schema"}\n'), "receive_failed"), ((0, b'{"result":"queued"}\n'), "bad_reply")],
)
def test_receive_errors_are_reported(tmp_path: Path, reply: tuple[int, bytes], code: str) -> None:
    h = Harness(tmp_path)
    h.stack.receive_reply = reply
    h.watcher().step()
    assert h.stack.failed() == [code]


def test_convert_failure_codes_are_reported(tmp_path: Path) -> None:
    cli = need("reader.ingest.host.cli")
    for code in ("convert_failed", "memory_limit", "work_limit", "timeout"):
        (tmp_path / code).mkdir()
        h = Harness(tmp_path / code)

        def failing(_: Path, code: str = code) -> None:
            raise cli.Stop(code, 5)

        h.convert_hook = failing
        h.watcher().step()
        assert h.stack.failed() == [code]
        assert h.events[-2:] == ["awake-stop", "unlock"]


def test_interrupt_stops_caffeinate_and_cleans(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    cli = need("reader.ingest.host.cli")

    def interrupted(_: Path) -> None:
        raise cli.Stop("interrupted", 143)

    h.convert_hook = interrupted
    with pytest.raises(cli.Stop):
        h.watcher().step()
    assert h.events[-2:] == ["awake-stop", "unlock"] and work_left(h) == []
    assert h.stack.failed() == []  # the lease expires and the upload returns to queued


def test_watch_logs_no_filename_or_text(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """FR-INGEST-24: watch-mode log lines hold ids, sizes, codes and timings only."""
    caplog.set_level(logging.DEBUG)
    h = Harness(tmp_path)
    h.watcher().step()
    assert "QUOKKA" not in caplog.text and "%PDF" not in caplog.text
    assert UPLOAD in caplog.text


# ---- caffeinate (FR-INGEST-21) ----


def test_caffeinate_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    watch = need("reader.ingest.host.watch")
    seen: dict[str, Any] = {}

    class FakePopen:
        def __init__(self, argv: list[str], **kw: Any) -> None:
            seen.update(argv=argv, **kw)
            self.killed = self.waited = False

        def kill(self) -> None:
            seen["killed"] = True

        def wait(self, timeout: float | None = None) -> int:
            seen["waited"] = True
            return 0

    monkeypatch.setenv("SECRET_CANARY", "x")
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    awake = watch.start_caffeinate(os.getpid())
    assert seen["argv"] == ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())]
    assert set(seen["env"]) <= {"PATH", "HOME", "LANG", "DOCKER_HOST"} and "SECRET_CANARY" not in seen["env"]
    assert seen["close_fds"] is True
    assert seen["stdin"] == seen["stdout"] == seen["stderr"] == subprocess.DEVNULL
    awake.stop()
    assert seen["killed"] and seen["waited"]


# ---- FR-INGEST-22: one converter run at a time; stale cleanup on taking the lock ----


def test_convert_lock_is_shared_by_cli_and_watcher(tmp_path: Path) -> None:
    watch = need("reader.ingest.host.watch")
    cli = need("reader.ingest.host.cli")
    held = watch.try_convert_lock(tmp_path)
    assert held is not None
    assert watch.try_convert_lock(tmp_path) is None
    with pytest.raises(cli.Stop) as busy, cli.convert_lock(tmp_path):
        pass
    assert (busy.value.code, busy.value.exit_code) == ("busy", 3)
    held.release()
    with cli.convert_lock(tmp_path):
        assert watch.try_convert_lock(tmp_path) is None


def test_watcher_claim_after_a_crashed_cli_cleans_its_record(tmp_path: Path) -> None:
    watch = need("reader.ingest.host.watch")
    records = need("reader.ingest.host.records")
    run_dir, work_root = tmp_path / "ingest" / "run", tmp_path / "ingest" / "work"
    name = records.new_run(run_dir, work_root, "a" * 64)
    records.set_child(run_dir, name, 99999, "Mon Nov 23 10:00:00 2026")

    class Probe:
        killed: ClassVar[list[int]] = []

        def alive(self, pid: int) -> bool:
            return True

        def start_time(self, pid: int) -> str:
            return "Mon Nov 23 10:00:00 2026"

        def cwd(self, pid: int) -> str:
            return os.path.realpath(work_root / name)

        def killpg(self, pid: int) -> None:
            Probe.killed.append(pid)

    assert watch.clean_all(tmp_path, Probe()) == 1
    assert Probe.killed == [99999]
    assert list(run_dir.iterdir()) == [] and list(work_root.iterdir()) == []


# ---- INV-INGEST-4: the cross-process probes at run time ----


def test_runtime_probe_copy_is_the_test_module() -> None:
    probe = need("reader.ingest.host.crossprobe")
    here = Path(__file__).resolve().parent / "cross_process_probe.py"
    assert probe.PROBE_SOURCE.read_bytes() == here.read_bytes()


@pytest.mark.parametrize(
    ("result", "state"),
    [
        ((0, {"other": {"kern.procargs2": "denied:errno 1", "proc_pidpath": "denied:errno 1"}}), "ready"),
        (
            (0, {"other": {"kern.procargs2": "allowed:42 bytes", "proc_pidpath": "denied:errno 1"}}),
            "setup_missing",
        ),
        ((0, {"other": {"kern.procargs2": "denied:errno 22"}}), "setup_missing"),
        ((1, None), "setup_missing"),
        ((0, None), "setup_missing"),
    ],
)
def test_runtime_probe_decides_the_host_state(
    caplog: pytest.LogCaptureFixture, result: tuple[int, Any], state: str
) -> None:
    probe = need("reader.ingest.host.crossprobe")
    caplog.set_level(logging.INFO)
    code, payload = result
    raw = b"" if payload is None else json.dumps(payload).encode()
    assert probe.judge(code, raw) == state
    if payload and state == "setup_missing" and code == 0:
        assert "kern.procargs2" in caplog.text and "allowed:42" not in caplog.text


# ---- code review round 1 ----


class ShellRunner:
    """The real SystemRunner, with the fetch exec replaced by a shell script (no Docker)."""

    def __init__(self, script: str) -> None:
        self.script = script

    def run(self, argv: list[str], env: dict[str, str], timeout: float, limit: int,
            stdin: bytes | None = None, sink: Callable[[bytes], None] | None = None) -> Any:
        execio = need("reader.ingest.host.execio")
        shell = ["/bin/sh", "-c", self.script]
        return execio.SystemRunner().run(shell, {"PATH": "/usr/bin:/bin"}, timeout, limit, stdin, sink)


def _fetch_through(tmp_path: Path, script: str) -> Any:
    watch = need("reader.ingest.host.watch")
    tmp_path.mkdir(parents=True, exist_ok=True)
    h = Harness(tmp_path)
    deps = h.deps()
    deps.runner = ShellRunner(script)
    claim = watch.Claim(id=UPLOAD, sha256=hashlib.sha256(BODY).hexdigest(), size=len(BODY), claim=CLAIM)
    target = tmp_path / "work" / "in" / "material.pdf"
    target.parent.mkdir(parents=True)
    return watch.Watcher(deps)._fetch("cid", claim, target)


def test_a_refused_fetch_is_read_through_the_real_runner(tmp_path: Path) -> None:
    watch = need("reader.ingest.host.watch")
    with pytest.raises(watch.Cancelled):
        _fetch_through(tmp_path / "a", "printf '{\"cancel\":true}\\n'; exit 3")
    assert _fetch_through(tmp_path / "b", "printf '{\"error\":\"missing\"}\\n'; exit 1") == "inbox_missing"


def test_setup_models_are_hashed_at_start_then_only_on_change(tmp_path: Path) -> None:
    watch = need("reader.ingest.host.watch")
    clock = Clock()
    hashed, mark, record = [0], ["a"], [None]

    def full() -> str | None:
        hashed[0] += 1
        return None

    check = watch.SetupCheck(
        record=lambda: record[0], full=full, fingerprint=lambda: mark[0], clock=clock.time
    )
    for _ in range(20):  # idle steps: the cheap record comparison only
        assert check.problem() is None
        clock.sleep(25)
    assert hashed == [1]
    mark[0] = "b"  # a model file's size or mtime changed
    check.problem()
    assert hashed == [2]
    clock.sleep(watch.SETUP_RECHECK_S)
    check.problem()
    assert hashed == [3]
    record[0] = "setup_stale"  # the record differs: reported at once, no hashing
    assert check.problem() == "setup_stale"
    assert hashed == [3]


def test_heartbeat_cancel_aborts_without_the_converter_raising(tmp_path: Path) -> None:
    h = Harness(tmp_path)

    def cancelled_mid_run(_: Path) -> None:
        h.stack.progress_reply = (3, CANCEL)
        h.timers[-1].fn()  # the heartbeat gets the cancel shape; the converter returns normally

    h.convert_hook = cancelled_mid_run
    h.watcher().step()
    assert "abort" in h.events
    assert h.stack.failed() == []
    assert "reader.ingest.receive" not in h.stack.commands()
    assert not any("--phase" in a and a[a.index("--phase") + 1] == "sending" for a in h.stack.calls)


def test_a_cancel_during_the_fetch_never_starts_the_converter(tmp_path: Path) -> None:
    h = Harness(tmp_path)

    def beat_cancelled() -> None:
        h.stack.progress_reply = (3, CANCEL)
        h.timers[-1].fn()

    h.stack.on_fetch = beat_cancelled
    h.watcher().step()
    assert "convert" not in h.events
    assert h.stack.failed() == []
    assert work_left(h) == []


def test_an_oserror_in_a_run_fails_the_claim_and_the_loop_continues(tmp_path: Path) -> None:
    h = Harness(tmp_path)

    def disk_full(_: Path) -> None:
        raise OSError(28, "No space left on device")

    h.convert_hook = disk_full
    h.watcher().step()
    assert h.stack.failed() == ["convert_failed"]
    assert h.events[-2:] == ["awake-stop", "unlock"]
    (tmp_path / "second").mkdir()
    h2 = Harness(tmp_path / "second")
    h2.stack.raise_on = "next"
    at = h2.clock.now
    h2.watcher().step()  # an OSError outside a claim: logged, then 10 s, never raised
    assert h2.clock.now - at >= 10


def test_a_setup_failure_releases_what_exists_and_fails_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 2: the run record's creation raises; nothing leaks and the claim is failed exactly once."""
    records = need("reader.ingest.host.records")
    h = Harness(tmp_path)

    def no_record(*_: Any, **__: Any) -> str:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(records, "new_run", no_record)
    w = h.watcher()
    w.step()
    assert len(h.timers) == 1 and h.timers[0].cancelled
    assert "awake-stop" in h.events
    assert h.events.index("caffeinate") < h.events.index("awake-stop")
    assert "convert" not in h.events
    assert w.busy == "none"
    assert h.stack.failed() == ["convert_failed"]


def test_a_code_outside_fail_s_list_is_reported_as_convert_failed(tmp_path: Path) -> None:
    cli = need("reader.ingest.host.cli")
    h = Harness(tmp_path)

    def blind(_: Path) -> None:
        raise cli.Stop("watchdog_failed", 5)

    h.convert_hook = blind
    h.watcher().step()
    assert h.stack.failed() == ["convert_failed"]


def test_a_status_probe_does_not_make_the_lock_busy(tmp_path: Path) -> None:
    import fcntl
    import threading

    watch = need("reader.ingest.host.watch")
    locks = tmp_path / "ingest" / "locks"
    locks.mkdir(parents=True)
    probe = os.open(locks / "convert.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)  # `reader status` probing at that moment
    threading.Timer(0.05, lambda: os.close(probe)).start()
    held = watch.try_convert_lock(tmp_path)
    assert held is not None
    held.release()
