"""FR-CLI-22: `reader up` starts the ingest watcher, `reader status` reports it, `reader down` stops it.

@trace FR-CLI-22
"""

import fcntl
import json
import os
import signal
import stat
import sys
import textwrap
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from .conftest import FIXTURES, World, make_world, rendered, stack, write_config

SHA = "c" * 64
OTHER = "d" * 64
ALLOWED = {"PATH", "HOME", "LANG", "DOCKER_HOST"}


def _config(tmp_path: Path) -> Path:
    """VEC-CLI-1 with the op reference on the keychain and the hostfile pointing at a real 0600 file."""
    secret = tmp_path / "secrets" / "anna-api-key"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("CANARY-hf")
    secret.chmod(0o600)
    text = (
        (FIXTURES / "config.yaml")
        .read_text()
        .replace("secret://op/Private/anna-claude/token", "secret://keychain/x/y")
        .replace("secret://hostfile/tmp/reader-ai-test/anna-api-key", "secret://hostfile" + str(secret.resolve()))
    )
    return write_config(tmp_path / "cfg", text)


def _data(config: Path) -> Path:
    return config.parent / "data"


@contextmanager
def watch_lock(data: Path, content: str) -> Iterator[int]:
    """Hold <data_dir>/ingest/locks/watch.lock as a running watcher does, with `content` inside."""
    locks = data / "ingest" / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    fd = os.open(locks / "watch.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.ftruncate(fd, 0)
    os.write(fd, content.encode())
    try:
        yield fd
    finally:
        os.close(fd)


def lock_line(pid: int, sha: str = SHA, busy: str = "none") -> str:
    return json.dumps({"pid": pid, "sha": sha, "busy": busy})


@pytest.fixture
def mod(monkeypatch: pytest.MonkeyPatch) -> Any:
    import importlib

    module = importlib.import_module("reader.cli.ingest_watch")
    monkeypatch.setattr(module, "checkout_sha", lambda: SHA)
    return module


class Spawns:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, str], Path]] = []
        self.fail = False

    def __call__(self, argv: list[str], env: dict[str, str], log_path: Path) -> int:
        if self.fail:
            raise OSError("no such file")
        self.calls.append((argv, dict(env), log_path))
        return 4321


def _world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mod: Any, **kw: Any) -> tuple[World, Spawns]:
    world = make_world(tmp_path, **kw)
    world.keyring.items[("reader-ai-system", "postgres")] = "CANARY-db"
    world.keyring.items[("reader-ai", "claude-oauth-token")] = "CANARY-kc"
    world.keyring.items[("x", "y")] = "CANARY-xy"
    monkeypatch.setenv("READER_TEST_TOKEN", "CANARY-env")
    stack(world, rendered(agents=True), volume_exists=True)
    spawns = Spawns()
    world.ctx.ingest_spawn = spawns
    return world, spawns


def _up(world: World, config: Path) -> int:
    from reader.cli.commands import cmd_up

    return int(cmd_up(world.ctx, config))


# ---- reader up ----


def test_up_starts_the_ingest_watcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mod: Any) -> None:
    world, spawns = _world(tmp_path, monkeypatch, mod)
    config = _config(tmp_path)
    assert _up(world, config) == 0
    assert len(spawns.calls) == 1
    argv, env, log_path = spawns.calls[0]
    assert argv == [str(Path(sys.executable).with_name("reader-ingest")), "--watch", "--config", str(config)]
    assert set(env) <= ALLOWED and env == {k: v for k, v in world.ctx.env.items() if k in ALLOWED}
    assert log_path == world.ctx.run_dir / "ingest-watch.log"


def test_up_leaves_a_watcher_of_the_same_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mod: Any) -> None:
    world, spawns = _world(tmp_path, monkeypatch, mod)
    config = _config(tmp_path)
    with watch_lock(_data(config), lock_line(4242)):
        assert _up(world, config) == 0
    assert spawns.calls == [] and (4242, signal.SIGTERM) not in world.kills


def test_up_restarts_an_idle_watcher_of_other_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mod: Any) -> None:
    config = _config(tmp_path)
    held: list[int] = []

    def release(pid: int, sig: int) -> None:
        if pid == 4242 and sig == signal.SIGTERM and held:
            os.close(held.pop())

    world, spawns = _world(tmp_path, monkeypatch, mod, kill=release)
    locks = _data(config) / "ingest" / "locks"
    locks.mkdir(parents=True)
    fd = os.open(locks / "watch.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.write(fd, lock_line(4242, OTHER).encode())
    held.append(fd)
    assert _up(world, config) == 0
    assert (4242, signal.SIGTERM) in world.kills and len(spawns.calls) == 1


@pytest.mark.parametrize("content", [lock_line(4242, OTHER, "claim"), lock_line(4242, OTHER, "cli_running"), '{"pid": 42', ""])
def test_up_leaves_a_busy_watcher_of_other_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mod: Any, content: str
) -> None:
    world, spawns = _world(tmp_path, monkeypatch, mod)
    config = _config(tmp_path)
    with watch_lock(_data(config), content):
        assert _up(world, config) == 0
        assert mod.state(_data(config), SHA) == "stale"
    assert spawns.calls == [] and not [k for k in world.kills if k[0] == 4242]
    assert "ingest watcher runs older code; it restarts at the next `reader up` when idle" in world.out


def test_a_failed_spawn_does_not_fail_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mod: Any) -> None:
    world, spawns = _world(tmp_path, monkeypatch, mod)
    spawns.fail = True
    assert _up(world, _config(tmp_path)) == 0
    assert "ingest watcher" in world.err


# ---- reader status ----


def test_status_reports_the_watcher_and_the_converter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mod: Any) -> None:
    from reader.cli.commands import cmd_status

    world, _ = _world(tmp_path, monkeypatch, mod)
    config = _config(tmp_path)
    data = _data(config)
    assert (mod.state(data, SHA), mod.converter(data)) == ("stopped", "idle")
    cmd_status(world.ctx, config, as_json=True)
    got = json.loads(world.out.splitlines()[-1])
    assert (got["ingest_watcher"], got["converter"]) == ("stopped", "idle")
    with watch_lock(data, lock_line(4242)):
        assert mod.state(data, SHA) == "running"
        assert mod.converter(data) == "idle"  # an idle watcher waits without convert.lock
        locks = data / "ingest" / "locks"
        fd = os.open(locks / "convert.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            assert mod.converter(data) == "running"
            cmd_status(world.ctx, config, as_json=True)
            got = json.loads(world.out.splitlines()[-1])
            assert (got["ingest_watcher"], got["converter"]) == ("running", "running")
        finally:
            os.close(fd)


# ---- reader down ----


def test_down_signals_only_a_held_lock_then_waits_15_s(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mod: Any) -> None:
    from reader.cli.commands import cmd_down

    world, _ = _world(tmp_path, monkeypatch, mod)
    config = _config(tmp_path)
    data = _data(config)
    locks = data / "ingest" / "locks"
    locks.mkdir(parents=True)
    (locks / "watch.lock").write_text(lock_line(4242))  # not held: a stale file
    cmd_down(world.ctx, config)
    assert not [k for k in world.kills if k[0] == 4242]
    with watch_lock(data, lock_line(4243)):
        start = world.clock.monotonic()
        cmd_down(world.ctx, config)
        assert [k for k in world.kills if k[0] == 4243] == [(4243, signal.SIGTERM), (4243, signal.SIGKILL)]
        assert world.clock.monotonic() - start >= 15
    downs = [i for i, e in enumerate(world.journal) if e[0] == "run" and "down" in e]
    kills = [i for i, e in enumerate(world.journal) if e[:2] == ("kill", signal.SIGKILL)]
    assert downs and kills and kills[-1] < downs[-1]  # stopped before `docker compose down`


# ---- the spawn itself (real processes) ----

FAKE = textwrap.dedent(
    """\
    #!{python}
    import json, os, sys
    fds = []
    for fd in range(64):
        try:
            os.fstat(fd)
            fds.append(fd)
        except OSError:
            pass
    try:
        os.close(os.open("/dev/tty", os.O_RDWR))
        tty = True
    except OSError:
        tty = False
    null = os.stat("/dev/null")
    st = os.fstat(0)
    report = dict(env=dict(os.environ), fds=fds, sid=os.getsid(0) == os.getpid(), tty=tty,
                  stdin_null=(st.st_rdev, st.st_ino) == (null.st_rdev, null.st_ino), argv=sys.argv[1:])
    open({out!r}, "w").write(json.dumps(report))
    print("to the log")
    """
)


def test_spawn_hygiene(tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "report.json"
    script = tmp_path / "reader-ingest"
    script.write_text(FAKE.format(python=sys.executable, out=str(out)))
    script.chmod(0o755)
    run = tmp_path / "run"
    run.mkdir(mode=0o700)
    log = run / "ingest-watch.log"
    log.write_bytes(b"o" * (5 << 20) + b"KEEP-THE-TAIL")
    os.chmod(log, 0o644)
    r, w = os.pipe()
    os.set_inheritable(r, True)
    os.set_inheritable(w, True)
    monkeypatch.setenv("SECRET_CANARY", "x")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LANG": "C.UTF-8"}
    try:
        pid = mod.spawn([str(script), "--watch"], env, log)
        _, status = os.waitpid(pid, 0)
    finally:
        os.close(r)
        os.close(w)
    report = json.loads(out.read_text())
    assert set(report["env"]) - {"__CF_USER_TEXT_ENCODING"} == set(env)
    assert "SECRET_CANARY" not in report["env"]
    assert report["fds"] == [0, 1, 2]
    assert report["sid"] is True and report["tty"] is False and report["stdin_null"] is True
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    data = log.read_bytes()
    assert len(data) <= (5 << 20) + 64 and b"KEEP-THE-TAIL" in data and data.endswith(b"to the log\n")


def test_spawn_creates_the_log_0600(tmp_path: Path, mod: Any) -> None:
    run = tmp_path / "run"
    run.mkdir(mode=0o700)
    pid = mod.spawn(["/usr/bin/true"], {"PATH": "/usr/bin:/bin"}, run / "ingest-watch.log")
    os.waitpid(pid, 0)
    assert stat.S_IMODE((run / "ingest-watch.log").stat().st_mode) == 0o600
