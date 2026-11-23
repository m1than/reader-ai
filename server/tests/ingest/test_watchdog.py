"""Host watchdogs over a converter run's processes (SRV-INGEST FR-INGEST-22 [unit, fake ps] and [host]).

@trace FR-INGEST-22 FR-INGEST-1
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from .conftest import need

PY = "/uv/python/cpython-3.14/bin/python3.14"  # the kernel executable path every converter process reports
VENV_PY = "/data/ingest/venv-x/bin/python"  # what `ps -o comm=` shows for the converter (unresolved)
WORK = "/data/ingest/work/0123456789abcdef"
T0 = "Mon Nov 23 10:00:00 2026"
T1 = "Mon Nov 23 10:00:05 2026"
EARLY = "Mon Nov 23 09:00:00 2026"
GiB = 1 << 30


@dataclass
class P:
    pid: int
    ppid: int = 1
    start: str = T1
    rss_kb: int = 1024
    comm: str = VENV_PY
    sid: int = 0
    exe: str = PY
    cwd: str = "/"
    args: str = ""


@dataclass
class FakeInspect:
    procs: dict[int, P]
    killed: list[int] = field(default_factory=list)
    changed: dict[int, P] = field(default_factory=dict)  # what a pid looks like at kill time

    def snapshot(self) -> list[Any]:
        wd = need("reader.ingest.host.watchdog")
        return [wd.Proc(p.pid, p.ppid, p.start, p.rss_kb, p.comm) for p in self.procs.values()]

    def _now(self, pid: int) -> P | None:
        return self.changed.get(pid, self.procs.get(pid))

    def sid(self, pid: int) -> int | None:
        p = self._now(pid)
        return p.sid if p else None

    def exe(self, pid: int) -> str | None:
        p = self._now(pid)
        return p.exe if p else None

    def cwd(self, pid: int) -> str | None:
        p = self._now(pid)
        return p.cwd if p else None

    def args(self, pid: int) -> str | None:
        p = self._now(pid)
        return p.args if p else None

    def start(self, pid: int) -> str | None:
        p = self._now(pid)
        return p.start if p else None

    def kill(self, pid: int) -> None:
        self.killed.append(pid)


def run(sid: int = 100, work: str = WORK, exclude: tuple[int, ...] = (1, 50)) -> Any:
    wd = need("reader.ingest.host.watchdog")
    return wd.Run(child_sid=sid, child_start=T0, work=work, exclude=frozenset(exclude))


def members(procs: list[P], r: Any = None) -> set[int]:
    wd = need("reader.ingest.host.watchdog")
    inspect = FakeInspect({p.pid: p for p in procs})
    return set(wd.members(r or run(), inspect.snapshot(), inspect, PY))


def test_membership_arms() -> None:
    procs = [
        P(100, ppid=50, sid=100, cwd=WORK, start=T0),  # the child
        P(101, ppid=100, sid=100, cwd=WORK),  # setpgid only: same session
        P(102, sid=102, cwd=WORK + "/tmp"),  # setsid, cwd under WORK
        P(103, sid=103, cwd="/", args=f"x --input {WORK}/in/material.pdf"),  # setsid, WORK in args
        P(104, sid=104, cwd="/", args="x --input /elsewhere/in/material.pdf"),  # not bound to this WORK
        P(105, sid=105, cwd=WORK, start=EARLY),  # started before the child
        P(106, sid=106, cwd=WORK, exe="/bin/zsh", comm="-zsh"),  # a shell in WORK: not the converter's Python
        P(50, ppid=1, sid=50, cwd=WORK),  # the watcher (excluded) even with its cwd in WORK
        P(107, sid=107, cwd=WORK + "x/tmp"),  # a sibling path that only shares the prefix
    ]
    assert members(procs) == {100, 101, 102, 103}


def test_two_live_runs_each_count_only_their_own() -> None:
    other = "/data/ingest/work/fedcba9876543210"
    procs = [
        P(100, sid=100, cwd=WORK, start=T0),
        P(102, sid=102, cwd=WORK),
        P(200, sid=200, cwd=other, start=T0),
        P(202, sid=202, cwd="/", args=f"--out {other}/out"),
    ]
    assert members(procs, run(100, WORK)) == {100, 102}
    assert members(procs, run(200, other)) == {200, 202}


def test_a_work_path_with_a_space_matches_by_substring() -> None:
    spaced = "/Users/x/Application Support/reader/ingest/work/0123456789abcdef"
    procs = [P(100, sid=100, cwd=spaced, start=T0), P(103, sid=103, args=f"py -m c --input {spaced}/in/material.pdf")]
    assert members(procs, run(100, spaced)) == {100, 103}


def test_limits_kill_every_member_with_their_code(tmp_path: Path) -> None:
    wd = need("reader.ingest.host.watchdog")
    procs = {
        100: P(100, sid=100, cwd=WORK, start=T0, rss_kb=5 * GiB // 1024),
        102: P(102, sid=102, cwd=WORK, rss_kb=4 * GiB // 1024),
        300: P(300, sid=300, cwd="/", rss_kb=20 * GiB // 1024),  # not a member: never counted
    }
    inspect = FakeInspect(procs)
    dog = wd.Watchdog(run(), inspect, PY, rss_max=8 * GiB, work_max=2 * GiB, work=tmp_path)
    assert dog.check() == "memory_limit"
    assert sorted(inspect.killed) == [100, 102]
    procs[102].rss_kb = 1024
    inspect.killed.clear()
    assert dog.check() is None and inspect.killed == []


def test_work_directory_limit(tmp_path: Path) -> None:
    wd = need("reader.ingest.host.watchdog")
    (tmp_path / "big").write_bytes(b"x" * 3000)
    (tmp_path / "link").symlink_to("/etc/passwd")  # not followed, not counted
    assert wd.work_bytes(tmp_path) == 3000
    inspect = FakeInspect({100: P(100, sid=100, cwd=WORK, start=T0)})
    dog = wd.Watchdog(run(), inspect, PY, rss_max=8 * GiB, work_max=2000, work=tmp_path)
    assert dog.check() == "work_limit" and inspect.killed == [100]
    many = tmp_path / "many"
    many.mkdir()
    for i in range(30):
        (many / str(i)).touch()
    assert wd.work_bytes(tmp_path, max_entries=20) > 10**15  # past the entry bound counts as over


def test_kills_are_reverified(tmp_path: Path) -> None:
    wd = need("reader.ingest.host.watchdog")
    procs = {100: P(100, sid=100, cwd=WORK, start=T0), 102: P(102, sid=102, cwd=WORK), 103: P(103, sid=103, cwd=WORK)}
    inspect = FakeInspect(procs)
    inspect.changed[102] = P(102, sid=102, cwd=WORK, start="Mon Nov 23 11:00:00 2026")  # pid reused
    inspect.changed[103] = P(103, sid=103, cwd=WORK, exe="/bin/zsh")  # exec'd something else
    dog = wd.Watchdog(run(), inspect, PY, rss_max=8 * GiB, work_max=2 * GiB, work=tmp_path)
    dog.kill_all()
    assert inspect.killed == [100]


def test_stand_in_sets_of_two_runs_end_separately(tmp_path: Path) -> None:
    """[unit, stand-ins] the one-run lock makes two real runs impossible; the function still keeps them apart."""
    wd = need("reader.ingest.host.watchdog")
    other = "/data/ingest/work/fedcba9876543210"
    procs = {
        100: P(100, sid=100, cwd=WORK, start=T0),
        102: P(102, sid=102, cwd=WORK),
        200: P(200, sid=200, cwd=other, start=T0),
        202: P(202, sid=202, cwd=other),
    }
    inspect = FakeInspect(procs)
    wd.Watchdog(run(100, WORK), inspect, PY, rss_max=8 * GiB, work_max=2 * GiB, work=tmp_path).kill_all()
    assert sorted(inspect.killed) == [100, 102]


def test_strays_are_counted_never_killed() -> None:
    wd = need("reader.ingest.host.watchdog")
    procs = [
        P(400, comm="/Users/x/.local/share/uv/python/cpython-3.14/bin/python3.14"),  # another tool's python
        P(401, comm="/Users/x/reader-ai/server/.venv/bin/python3"),  # the secret watcher
        P(402, comm=VENV_PY),  # a detached converter python outside any live run
        P(403, comm=VENV_PY, ppid=77),  # not detached
        P(100, comm=VENV_PY, sid=100),  # a member of a live run
    ]
    inspect = FakeInspect({p.pid: p for p in procs})
    assert wd.strays(inspect.snapshot(), VENV_PY, live={100}) == 1
    assert inspect.killed == []


def test_ps_snapshot_parsing() -> None:
    wd = need("reader.ingest.host.watchdog")
    out = (
        "  100     1 Mon Nov 23 10:00:00 2026  2048 /data/ingest/venv-x/bin/python\n"
        "  101   100 Mon Nov 23 10:00:05 2026     0 /path with space/bin/python3.14\n"
    )
    got = wd.parse_ps(out)
    assert got == [
        wd.Proc(100, 1, "Mon Nov 23 10:00:00 2026", 2048, "/data/ingest/venv-x/bin/python"),
        wd.Proc(101, 100, "Mon Nov 23 10:00:05 2026", 0, "/path with space/bin/python3.14"),
    ]


def test_tick_timing_is_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    wd = need("reader.ingest.host.watchdog")
    caplog.set_level(logging.INFO)
    inspect = FakeInspect({100: P(100, sid=100, cwd=WORK, start=T0)})
    wd.Watchdog(run(), inspect, PY, rss_max=8 * GiB, work_max=2 * GiB, work=tmp_path).check()
    assert "watchdog_tick" in caplog.text and "ms" in caplog.text


# ---- the CLI's converter run uses the watchdog too ----


def test_cli_converter_run_stops_at_a_watchdog_limit(tmp_path: Path) -> None:
    cli = need("reader.ingest.host.cli")
    killed: list[str] = []

    class Child:
        pid = 4321

        def stdout_lines(self) -> Any:
            yield b'{"pages_done": 1, "pages_total": 2}\n'
            time.sleep(0.05)
            yield b'{"pages_done": 2, "pages_total": 2}\n'

        def wait(self, timeout: float | None = None) -> int:
            return -9

        def terminate(self) -> None:
            killed.append("term")

        def kill(self) -> None:
            killed.append("kill")

    class Dog:
        def check(self) -> str | None:
            return "memory_limit"

        def kill_all(self) -> None:
            killed.append("kill_all")

    deps = cli.Deps(runner=None, spawn=lambda *a: Child(), probe=None, venv=tmp_path, pyroot=tmp_path, models=tmp_path)
    deps.watchdog = lambda child, work: Dog()
    deps.tick_s = 0.01
    work = tmp_path / "w"
    (work / "in").mkdir(parents=True)
    with pytest.raises(cli.Stop) as stop:
        cli.convert_members(work, deps, lambda _: None)
    assert (stop.value.code, stop.value.exit_code) == ("memory_limit", 5)
    assert "kill_all" in killed


# ---- [host] real processes ----

STAND_IN = textwrap.dedent(
    """
    import os, sys, time
    mode = sys.argv[1]
    pid = os.fork()
    if pid == 0:
        if mode == "setsid":
            os.setsid()
        else:
            os.setpgid(0, 0)
        hold = bytearray(64 << 20)
        for i in range(0, len(hold), 4096):
            hold[i] = 1
        time.sleep(60)
        os._exit(0)
    hold = bytearray(16 << 20)
    for i in range(0, len(hold), 4096):
        hold[i] = 1
    time.sleep(60)
    """
)


@pytest.mark.ingest_host
@pytest.mark.parametrize("mode", ["setsid", "setpgid"])
def test_escaping_children_are_found_and_killed(tmp_path: Path, mode: str) -> None:
    if os.environ.get("READER_TEST_INGEST_HOST") != "1":
        pytest.skip("[host]: set READER_TEST_INGEST_HOST=1")
    wd = need("reader.ingest.host.watchdog")
    records = need("reader.ingest.host.records")
    work = Path(os.path.realpath(tmp_path)) / "work"
    work.mkdir()
    script = work / "stand_in.py"
    script.write_text(STAND_IN)
    child = subprocess.Popen([sys.executable, "-I", str(script), mode], cwd=work, start_new_session=True)  # noqa: S603
    try:
        time.sleep(1.5)
        start = records.SystemProbe().start_time(child.pid)
        r = wd.Run(child_sid=child.pid, child_start=start, work=str(work), exclude=frozenset({os.getpid(), os.getppid()}))
        inspect = wd.SystemInspect()
        found = wd.members(r, inspect.snapshot(), inspect, os.path.realpath(sys.executable))
        assert child.pid in found and len(found) == 2, found
        dog = wd.Watchdog(r, inspect, os.path.realpath(sys.executable), rss_max=40 << 20, work_max=2 * GiB, work=work)
        assert dog.check() == "memory_limit"
        time.sleep(0.3)
        assert all(not _alive(pid) for pid in found)
    finally:
        with __import__("contextlib").suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        child.wait(5)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    out = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False)  # noqa: S603
    return bool(out.stdout.strip()) and not out.stdout.strip().startswith("Z")


PARENT = textwrap.dedent(
    """
    import json, sys, time
    from pathlib import Path
    from reader.ingest.host import cli, records
    data = Path(sys.argv[1])
    run_dir, work_root = data / "ingest" / "run", data / "ingest" / "work"
    with cli.convert_lock(data):
        name = records.new_run(run_dir, work_root, "a" * 64)
        work = work_root / name
        child = cli.system_spawn(
            ["/bin/sh", "-c", "ls /dev/fd > fds.txt; sleep 60"], {"PATH": "/usr/bin:/bin"}, work
        )
        records.set_child(run_dir, name, child.pid, records.SystemProbe().start_time(child.pid) or "")
        print(json.dumps({"pid": child.pid, "work": str(work)}), flush=True)
        time.sleep(60)
    """
)


@pytest.mark.ingest_host
def test_lock_is_not_inherited_and_an_orphan_is_cleaned(tmp_path: Path) -> None:
    if os.environ.get("READER_TEST_INGEST_HOST") != "1":
        pytest.skip("[host]: set READER_TEST_INGEST_HOST=1")
    watch = need("reader.ingest.host.watch")
    data = Path(os.path.realpath(tmp_path))
    parent = subprocess.Popen([sys.executable, "-c", PARENT, str(data)], stdout=subprocess.PIPE)  # noqa: S603
    assert parent.stdout is not None
    started = json.loads(parent.stdout.readline())
    time.sleep(0.5)
    assert watch.try_convert_lock(data) is None  # held by the parent
    lock_fds = (Path(started["work"]) / "fds.txt").read_text().split()
    parent.send_signal(signal.SIGKILL)
    parent.wait(5)
    try:
        assert _alive(started["pid"])  # the orphaned child still runs
        held = watch.try_convert_lock(data)
        assert held is not None  # ... but does not hold the lock
        assert len(lock_fds) <= 4  # 0, 1, 2 and ls's own directory fd: no inherited lock fd
        assert watch.clean_all(data, need("reader.ingest.host.records").SystemProbe()) == 1
        time.sleep(0.3)
        assert not _alive(started["pid"])
        held.release()
    finally:
        with __import__("contextlib").suppress(ProcessLookupError):
            os.killpg(started["pid"], signal.SIGKILL)
