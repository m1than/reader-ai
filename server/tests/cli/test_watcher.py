"""The watcher: bundle, delivery protocol, event trust, reconcile, resubscription, lock, stop, hardening, log.

@trace FR-CLI-5, FR-CLI-6, FR-CLI-7, FR-CLI-8, FR-CLI-9, FR-CLI-19, FR-CLI-20, VEC-CLI-4
@trace INV-CLI-3, INV-CLI-7, INV-CLI-8
"""

import json
import logging
import os
import signal
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from .conftest import FIXTURES, SCRIPT, FakeClock, FakeRunner, held_lock, load_yaml, make_world

EVENTS = load_yaml(FIXTURES / "events.yaml")
UIDS = {"api": 10001, "worker": 10001, "postgres": 999}
CANARY = "CANARY-watch-2b77"


def _deliveries() -> list[Any]:
    from reader.cli.stack import Delivery

    return [Delivery(s, UIDS[s], "db-password") for s in sorted(UIDS)]


def _watcher(runner: FakeRunner, clock: FakeClock | None = None, **kw: Any) -> Any:
    from reader.cli.watcher import Watcher
    from reader.config import Secret

    clock = clock or FakeClock()
    kw.setdefault("sleep", clock.sleep)
    return Watcher(
        runner=runner,
        env={"PATH": "/usr/bin"},
        project=EVENTS["project"],
        deliveries=_deliveries(),
        values={"db-password": Secret(CANARY)},
        submit=lambda job: job(),
        **kw,
    )


def _event(case_id: str) -> str:
    case = next(c for c in EVENTS["cases"] if c["id"] == case_id)
    return json.dumps(case["event"])


# bundle ---------------------------------------------------------------------------------------------------


def test_bundle_round_trips_length_prefixed_records() -> None:
    from reader.cli.watcher import decode_bundle, encode_bundle
    from reader.config import Secret

    values = {"db-password": Secret("a\nb"), "keychain-" + "0" * 32: Secret("x" * 70)}
    data = encode_bundle(values)
    assert {k: v.reveal() for k, v in decode_bundle(data).items()} == {
        k: v.reveal() for k, v in values.items()
    }


@pytest.mark.parametrize("data", [b"\x00\x00\x00\x05db-p", b"\x00\x00\x00\x02xx\x00\x00\x00\x01v"])
def test_bundle_rejects_truncated_records_and_bad_names(data: bytes) -> None:
    from reader.cli.watcher import decode_bundle

    with pytest.raises(ValueError, match="bundle"):
        decode_bundle(data)


# event trust (FR-CLI-9, VEC-CLI-4) ------------------------------------------------------------------------


@pytest.mark.parametrize("case", EVENTS["cases"], ids=lambda c: c["id"])
def test_event_target_checks_the_events_own_labels(case: dict[str, Any]) -> None:
    from reader.cli.watcher import event_target

    target = event_target(case["event"], EVENTS["project"], set(EVENTS["consumers"]))
    if case["deliveries"]:
        assert target == (
            case["event"]["Actor"]["ID"],
            case["event"]["Actor"]["Attributes"]["com.docker.compose.service"],
        )
    else:
        assert target is None


@pytest.mark.parametrize("case", EVENTS["cases"], ids=lambda c: c["id"])
def test_events_cause_exactly_the_fixture_deliveries(case: dict[str, Any]) -> None:
    runner = FakeRunner()
    _watcher(runner).handle_line(json.dumps(case["event"]))
    got = [{"container": c.argv[5], "uid": int(c.argv[4]), "name": c.argv[-1]} for c in runner.calls]
    assert got == case["deliveries"]


def test_foreign_project_event_runs_no_docker_exec() -> None:
    runner = FakeRunner()
    _watcher(runner).handle_line(_event("foreign-project"))
    assert runner.argvs("docker", "exec") == []


@pytest.mark.parametrize(
    ("attributes", "label"),
    [
        (
            {
                "com.docker.compose.project": "VALUE-proj-x",
                "com.docker.compose.oneoff": "False",
                "com.docker.compose.service": "api",
            },
            "com.docker.compose.project",
        ),
        (
            {
                "com.docker.compose.project": "reader-vec",
                "com.docker.compose.oneoff": "VALUE-True",
                "com.docker.compose.service": "api",
            },
            "com.docker.compose.oneoff",
        ),
        (
            {
                "com.docker.compose.project": "reader-vec",
                "com.docker.compose.oneoff": "False",
                "com.docker.compose.service": "VALUE-kernel",
            },
            "com.docker.compose.service",
        ),
    ],
)
def test_ignored_event_is_logged_at_info_with_the_label_name_only(
    attributes: dict[str, str], label: str, caplog: pytest.LogCaptureFixture
) -> None:
    runner = FakeRunner()
    event = {
        "Type": "container",
        "Action": "start",
        "Actor": {"ID": "deadbeef0123456789", "Attributes": attributes},
    }
    with caplog.at_level(logging.INFO, logger="reader.cli"):
        _watcher(runner).handle_line(json.dumps(event))
    (record,) = [r for r in caplog.records if "event_ignored" in r.getMessage()]
    assert record.levelno == logging.INFO
    assert label in record.getMessage()
    assert "deadbeef0123" in record.getMessage()
    assert "VALUE-" not in caplog.text
    assert runner.calls == []


def test_a_started_judge_lane_gets_its_db_password_again() -> None:
    """FR-CLI-5 amended (ADR-0039 B7b): worker-judge is a consumer; a start event schedules one db-password delivery."""
    from reader.cli.stack import Delivery
    from reader.cli.watcher import Watcher
    from reader.config import Secret

    runner = FakeRunner()
    w = Watcher(
        runner=runner, env={"PATH": "/usr/bin"}, project=EVENTS["project"],
        deliveries=[*_deliveries(), Delivery("worker-judge", 10001, "db-password")],
        values={"db-password": Secret(CANARY)}, submit=lambda job: job(), sleep=FakeClock().sleep,
    )
    attributes = {
        "com.docker.compose.project": EVENTS["project"],
        "com.docker.compose.oneoff": "False",
        "com.docker.compose.service": "worker-judge",
    }
    event = {"Type": "container", "Action": "start", "Actor": {"ID": "c0ffee", "Attributes": attributes}}
    w.handle_line(json.dumps(event))
    (call,) = runner.calls
    assert call.argv[:5] == ["docker", "exec", "-i", "-u", "10001"]
    assert call.argv[-1] == "db-password"


def test_garbage_event_lines_are_ignored() -> None:
    runner = FakeRunner()
    w = _watcher(runner)
    w.handle_line("not json")
    w.handle_line("[]")
    assert runner.calls == []


# delivery protocol (FR-CLI-6, INV-CLI-3) ------------------------------------------------------------------


def test_delivery_argv_and_stdin() -> None:
    runner = FakeRunner()
    _watcher(runner).handle_line(_event("own-consumer"))
    (call,) = runner.calls
    assert call.argv == [
        "docker",
        "exec",
        "-i",
        "-u",
        "10001",
        "c0ffee01",
        "sh",
        "-c",
        SCRIPT,
        "sh",
        "db-password",
    ]
    assert call.stdin == CANARY.encode()
    assert call.timeout == 10
    assert CANARY not in " ".join(call.argv)
    assert call.env is not None
    assert CANARY not in json.dumps(call.env)


def test_failed_delivery_is_retried_three_times_one_second_apart(caplog: pytest.LogCaptureFixture) -> None:
    runner = FakeRunner().on("docker", "exec", result=(1, b"", b"boom"))
    clock = FakeClock()
    with caplog.at_level(logging.DEBUG):
        _watcher(runner, clock).handle_line(_event("own-consumer"))
    assert len(runner.calls) == 4
    assert clock.slept == [1, 1, 1]
    assert "db-password" in caplog.text
    assert CANARY not in caplog.text


def test_timed_out_delivery_is_retried() -> None:
    from reader.cli.runner import CommandTimeout

    def hang(_call: Any) -> Any:
        raise CommandTimeout()

    runner = FakeRunner().on("docker", "exec", handler=hang)
    clock = FakeClock()
    _watcher(runner, clock).handle_line(_event("own-consumer"))
    assert len(runner.calls) == 4


def test_names_outside_the_pattern_are_never_delivered() -> None:
    from reader.cli.stack import Delivery
    from reader.cli.watcher import Watcher
    from reader.config import Secret

    runner = FakeRunner()
    w = Watcher(
        runner=runner,
        env={"PATH": "/usr/bin"},
        project="reader-vec",
        deliveries=[Delivery("api", 10001, "../etc/passwd")],
        values={"../etc/passwd": Secret("v")},
        sleep=FakeClock().sleep,
        submit=lambda job: job(),
    )
    with pytest.raises(ValueError, match="name"):
        w.deliver("c0ffee01", "api")
    assert runner.calls == []


def test_after_stop_no_new_delivery_or_retry_starts() -> None:
    runner = FakeRunner()
    w = _watcher(runner)

    def fail_then_stop(_call: Any) -> Any:
        w.stop()
        return 1

    runner.on("docker", "exec", handler=fail_then_stop)
    w.handle_line(_event("own-consumer"))
    assert len(runner.calls) == 1
    w.handle_line(_event("own-consumer"))
    assert len(runner.calls) == 1


def test_stop_during_the_retry_wait_starts_no_retry() -> None:
    runner = FakeRunner().on("docker", "exec", result=1)
    holder: list[Any] = []

    def sleep_then_stop(_seconds: float) -> None:
        holder[0].stop()

    w = _watcher(runner, sleep=sleep_then_stop)
    holder.append(w)
    w.handle_line(_event("own-consumer"))
    assert len(runner.calls) == 1


def test_default_wait_ends_at_once_on_stop() -> None:
    import time

    from reader.cli.watcher import Watcher
    from reader.config import Secret

    w = Watcher(
        runner=FakeRunner(),
        env={"PATH": "/usr/bin"},
        project="reader-vec",
        deliveries=_deliveries(),
        values={"db-password": Secret(CANARY)},
        submit=lambda job: job(),
    )
    w.stop()
    started = time.monotonic()
    w.sleep(30)
    assert time.monotonic() - started < 1


def test_each_container_is_one_job() -> None:
    jobs: list[Any] = []
    runner = FakeRunner()
    w = _watcher(runner)
    w.submit = jobs.append
    w.handle_line(_event("own-consumer"))
    assert len(jobs) == 1
    assert runner.calls == []
    jobs[0]()
    assert len(runner.calls) == 1


# reconcile and the event loop (FR-CLI-8, INV-CLI-8) -------------------------------------------------------


def _listing(runner: FakeRunner, labels: dict[str, dict[str, str]]) -> None:
    runner.on("docker", "ps", result="\n".join(labels).encode() + b"\n")
    runner.on(
        "docker",
        "inspect",
        result="".join(f"{cid} {json.dumps(lab)}\n" for cid, lab in labels.items()).encode(),
    )


def test_reconcile_delivers_to_running_consumers_after_checking_their_labels() -> None:
    runner = FakeRunner()
    _listing(
        runner,
        {
            "aaa": {
                "com.docker.compose.project": "reader-vec",
                "com.docker.compose.oneoff": "False",
                "com.docker.compose.service": "postgres",
            },
            "bbb": {
                "com.docker.compose.project": "other",
                "com.docker.compose.oneoff": "False",
                "com.docker.compose.service": "api",
            },
            "ccc": {
                "com.docker.compose.project": "reader-vec",
                "com.docker.compose.oneoff": "True",
                "com.docker.compose.service": "api",
            },
        },
    )
    assert _watcher(runner).reconcile() is True
    execs = runner.argvs("docker", "exec")
    assert [(a[4], a[5]) for a in execs] == [("999", "aaa")]
    (ps,) = runner.argvs("docker", "ps")
    assert "label=com.docker.compose.project=reader-vec" in ps


def test_run_subscribes_then_reconciles_then_signals_ready_then_follows_events() -> None:
    runner = FakeRunner()
    _listing(runner, {})
    runner.on("docker", "info", result=b"2026-11-20T10:00:05Z\n")
    runner.streams = [[_event("own-consumer")]]
    clock = FakeClock()
    w = _watcher(runner, clock)
    ready: list[int] = []

    def on_ready() -> None:
        ready.append(len(runner.journal))
        runner.journal.append(("ready",))

    code = w.run(ready=on_ready, since="2026-11-20T10:00:00Z")
    kinds = [e[0] if e[0] != "run" else " ".join(e[1:3]) for e in runner.journal]
    assert (
        kinds.index("stream") < kinds.index("docker ps") < kinds.index("ready") < kinds.index("docker exec")
    )
    first = runner.stream_calls[0]
    assert first.argv == [
        "docker",
        "events",
        "--since",
        "2026-11-20T10:00:00Z",
        "--filter",
        "type=container",
        "--filter",
        "event=start",
        "--filter",
        "label=com.docker.compose.project=reader-vec",
        "--format",
        "{{json .}}",
    ]
    assert len(ready) == 1
    assert clock.slept == [1, 2, 4]
    assert len(runner.stream_calls) == 4
    assert code != 0


def test_run_returns_zero_when_stopped() -> None:
    runner = FakeRunner()
    _listing(runner, {})
    w = _watcher(runner)
    runner.streams = [[]]
    w.stop()
    assert w.run(ready=lambda: None, since="t") == 0


def test_failed_reconcile_is_a_failed_subscription() -> None:
    runner = FakeRunner().on("docker", "ps", result=(1, b"", b"daemon gone"))
    runner.streams = [["{}"], ["{}"], ["{}"], ["{}"]]
    clock = FakeClock()
    ready: list[int] = []
    code = _watcher(runner, clock).run(ready=lambda: ready.append(1), since="T0")
    assert code != 0
    assert ready == []
    assert clock.slept == [1, 2, 4]
    assert {c.argv[3] for c in runner.stream_calls} == {"T0"}


class _Stream:
    """A stream that lives `seconds` on the fake clock, then ends."""

    def __init__(self, clock: FakeClock, seconds: float) -> None:
        self.clock = clock
        self.seconds = seconds

    def __iter__(self) -> Any:
        self.clock.now += self.seconds
        return iter([])

    def close(self) -> None:
        pass


def test_a_long_lived_stream_resets_the_failure_count() -> None:
    clock = FakeClock()
    runner = FakeRunner()
    _listing(runner, {})
    lives = [0, 0, 5, 0, 0, 0]
    opened: list[float] = []

    def stream(argv: Any, *, env: Any) -> Any:
        opened.append(lives[len(opened)])
        return _Stream(clock, opened[-1])

    runner.stream = stream  # type: ignore[method-assign]
    w = _watcher(runner, clock, monotonic=clock.monotonic)
    assert w.run(ready=lambda: None, since="T0") != 0
    assert clock.slept == [1, 2, 1, 2, 4]
    assert len(opened) == 6


# lock, status, stop (FR-CLI-7, INV-CLI-7) -----------------------------------------------------------------


def test_lock_records_the_pid_and_excludes_a_second_watcher(tmp_path: Path) -> None:
    from reader.cli.errors import CliError
    from reader.cli.watcher import acquire_lock, lock_holder

    lock = tmp_path / "watcher.lock"
    fd = acquire_lock(lock)
    try:
        assert lock_holder(lock) == os.getpid()
        with pytest.raises(CliError) as caught:
            acquire_lock(lock)
        assert caught.value.code == "watcher_running"
    finally:
        os.close(fd)
    assert lock_holder(lock) is None


def test_stop_never_signals_a_pid_whose_lock_is_not_held(tmp_path: Path) -> None:
    from reader.cli.watcher import stop_watcher

    world = make_world(tmp_path)
    world.ctx.run_dir.mkdir()
    (world.ctx.run_dir / "watcher.lock").write_text("12345")
    assert stop_watcher(world.ctx) is False
    assert world.kills == []


def test_stop_sends_sigterm_and_waits_for_the_lock(tmp_path: Path) -> None:
    from reader.cli.watcher import stop_watcher

    holder: list[Any] = []

    def release(_pid: int, sig: int) -> None:
        if sig == signal.SIGTERM:
            holder[0].release()

    world = make_world(tmp_path, kill=release)
    with held_lock(world.ctx.run_dir, pid=4242) as lock:
        holder.append(lock)
        assert stop_watcher(world.ctx) is True
    assert world.kills == [(4242, signal.SIGTERM)]


def test_stop_sends_sigkill_after_15_seconds(tmp_path: Path) -> None:
    from reader.cli.watcher import stop_watcher

    world = make_world(tmp_path)
    with held_lock(world.ctx.run_dir, pid=4242):
        stop_watcher(world.ctx)
    assert world.kills == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert sum(world.clock.slept) >= 15 + 5  # then it waits up to 5 s for the lock after SIGKILL
    assert sum(world.clock.slept) < 21


def test_a_status_probe_does_not_make_a_starting_watcher_fail(tmp_path: Path) -> None:
    import fcntl
    import threading

    from reader.cli.watcher import acquire_lock

    lock = tmp_path / "watcher.lock"
    lock.write_text("")
    probe = os.open(lock, os.O_RDONLY)
    fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)  # a probe caught mid-check
    timer = threading.Timer(0.2, lambda: os.close(probe))
    timer.start()
    fd = acquire_lock(lock)
    timer.join()
    os.close(fd)


def test_watcher_status_command(tmp_path: Path) -> None:
    from reader.cli.commands import cmd_watcher_status

    world = make_world(tmp_path)
    assert cmd_watcher_status(world.ctx) == 3
    assert world.out.strip() == "stopped"
    world.ctx.out.truncate(0)
    world.ctx.out.seek(0)
    with held_lock(world.ctx.run_dir):
        assert cmd_watcher_status(world.ctx) == 0
    assert world.out.strip() == "running"


# hardening and log (FR-CLI-20, FR-CLI-19) -----------------------------------------------------------------


def test_harden_sets_core_limit_umask_and_cwd(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    probe = (
        "import os,resource,sys\n"
        "from reader.cli.watcher import harden\n"
        "harden(__import__('pathlib').Path(sys.argv[1]))\n"
        "u=os.umask(0)\n"
        "print(resource.getrlimit(resource.RLIMIT_CORE)[0], oct(u), os.getcwd())\n"
    )
    done = subprocess.run(  # noqa: S603 - fixed argv: this interpreter and a constant probe
        [sys.executable, "-c", probe, str(run_dir)],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": os.environ.get("PATH", "/usr/bin")},
    )
    assert done.returncode == 0, done.stderr[-400:]
    out = done.stdout.split()
    assert out[0] == "0"
    assert out[1] == "0o77"
    assert Path(out[2]).resolve() == run_dir.resolve()


def test_secret_fields_cannot_be_logged() -> None:
    from reader.cli.log import log_event
    from reader.config import Secret

    with pytest.raises(TypeError):
        log_event(logging.getLogger("reader.cli"), "delivered", name="db-password", value=Secret(CANARY))


def test_watcher_log_is_0600_and_capped(tmp_path: Path) -> None:
    from logging.handlers import RotatingFileHandler

    from reader.cli.log import open_watcher_log

    handler = open_watcher_log(tmp_path / "logs")
    try:
        path = tmp_path / "logs" / "watcher.log"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert isinstance(handler, RotatingFileHandler)
        assert handler.maxBytes == 10 * 1024 * 1024
        assert handler.backupCount == 1
    finally:
        handler.close()
