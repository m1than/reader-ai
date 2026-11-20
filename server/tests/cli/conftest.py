"""Fakes and helpers for the OPS-CLI unit tests (spec/modules/OPS-CLI.yaml, test levels: [unit]).

Every subprocess goes through a fake runner that records argv, env, stdin and timeout; the keyring, the
watcher spawner, the clock and `kill` are fakes too. No docker daemon, no Keychain, no real signal.
The module under test (`reader.cli`) is imported inside test bodies and helper calls, never at import time,
so that in the Red phase every test fails on its own.
"""

import fcntl
import json
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


class _MissingCli:
    """Last meta-path finder: a `reader.cli` module no other finder knows is an assertion failure, not an
    ImportError, so a Red test fails on an assertion inside its body."""

    @staticmethod
    def find_spec(fullname: str, path: object = None, target: object = None) -> None:
        if fullname == "reader.cli" or fullname.startswith("reader.cli."):
            raise AssertionError(f"{fullname} does not exist (OPS-CLI not implemented)")


sys.meta_path.append(_MissingCli())

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "spec" / "fixtures" / "OPS-CLI"
DEPLOY_FIXTURES = REPO / "spec" / "fixtures" / "OPS-DEPLOY"
COMPOSE = REPO / "deploy" / "compose.yaml"
PYPROJECT = REPO / "server" / "pyproject.toml"
ENV_ALLOW = {"PATH", "HOME", "LANG", "DOCKER_HOST", "DOCKER_CONTEXT"}
SCRIPT = 'umask 077; cat > "/run/secrets/.$1.tmp" && mv "/run/secrets/.$1.tmp" "/run/secrets/$1"'
T0 = "2026-11-20T10:00:00.123456789Z"


def load_yaml(path: Path) -> Any:
    return YAML(typ="safe", pure=True).load(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------------------------------------
# Rendered models (excerpts of `docker compose config --format json`)


def _vol(source: str, target: str) -> dict[str, Any]:
    return {"type": "volume", "source": source, "target": target}


def rendered(name: str = "reader-vec", *, agents: bool = False, mcp: bool = False) -> dict[str, Any]:
    """A default-profile rendered model with mounts and labels; `agents`/`mcp` add those profile services.
    Like `docker compose config`, it omits services of inactive profiles (the kernels)."""
    services: dict[str, Any] = {
        "caddy": {
            "image": "caddy:2-alpine",
            "volumes": [_vol("caddy_data", "/data"), _vol("caddy_config", "/config")],
        },
        "postgres": {
            "image": "reader-postgres:local",
            "user": "999:999",
            "volumes": [_vol("pgdata", "/var/lib/postgresql")],
        },
        "api": {
            "image": "reader-server:local",
            "user": "10001:10001",
            "volumes": [_vol("blobs", "/var/lib/reader/blobs")],
        },
        "worker": {
            "image": "reader-server:local",
            "user": "10001:10001",
            "volumes": [_vol("blobs", "/var/lib/reader/blobs")],
        },
    }
    if mcp:
        services["mcp"] = {"image": "reader-server:local", "user": "10001:10001"}
    if agents:
        services["agent-proxy"] = {"image": "reader-agent-proxy:local", "user": "10002:10002"}
    labels = {"pgdata": "include", "blobs": "include", "caddy_data": "exclude", "caddy_config": "include"}
    return {
        "name": name,
        "services": services,
        "volumes": {
            k: {"name": f"{name}_{k}", "labels": {"org.reader-ai.backup": v}} for k, v in labels.items()
        },
    }


def ps_lines(states: dict[str, tuple[str, str]]) -> bytes:
    """`docker compose ps --format json` output, one JSON object per line."""
    return "".join(
        json.dumps({"Service": s, "State": st, "Health": h}) + "\n" for s, (st, h) in states.items()
    ).encode()


def healthy(model: dict[str, Any]) -> bytes:
    return ps_lines({s: ("running", "healthy") for s in model["services"]})


# ------------------------------------------------------------------------------------------------------------
# Fakes


@dataclass
class Call:
    argv: list[str]
    env: dict[str, str] | None
    stdin: bytes | None
    timeout: float | None


Handler = Callable[[Call], Any]


def _subsequence(tokens: tuple[str, ...], argv: list[str]) -> bool:
    it = iter(argv)
    return all(any(t == a for a in it) for t in tokens)


class FakeStream:
    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)
        self.closed = False

    def __iter__(self) -> Iterator[str]:
        for line in self._lines:
            if self.closed:
                return
            yield line

    def close(self) -> None:
        self.closed = True


class FakeRunner:
    """Records every call; answers with the newest rule whose tokens appear in argv in order (else exit 0)."""

    def __init__(self, journal: list[tuple[Any, ...]] | None = None) -> None:
        self.calls: list[Call] = []
        self.rules: list[tuple[tuple[str, ...], Any]] = []
        self.streams: list[list[str]] = []
        self.stream_calls: list[Call] = []
        self.journal = journal if journal is not None else []

    def on(self, *tokens: str, result: Any = None, handler: Handler | None = None) -> FakeRunner:
        self.rules.insert(0, (tokens, handler if handler is not None else (lambda _c: result)))
        return self

    def run(self, argv: Any, *, env: Any, stdin: bytes | None = None, timeout: float | None = None) -> Any:
        from reader.cli.runner import Result

        call = Call([str(a) for a in argv], None if env is None else dict(env), stdin, timeout)
        self.calls.append(call)
        self.journal.append(("run", *call.argv))
        for tokens, answer in self.rules:
            if _subsequence(tokens, call.argv):
                value = answer(call)
                if value is None:
                    return Result(0, b"", b"")
                if isinstance(value, bytes):
                    return Result(0, value, b"")
                if isinstance(value, int):
                    return Result(value, b"", b"")
                code, out, err = value
                return Result(code, out, err)
        return Result(0, b"", b"")

    def stream(self, argv: Any, *, env: Any) -> FakeStream:
        call = Call([str(a) for a in argv], None if env is None else dict(env), None, None)
        self.stream_calls.append(call)
        self.journal.append(("stream", *call.argv))
        lines = self.streams.pop(0) if self.streams else []
        return FakeStream(lines)

    def argvs(self, *tokens: str) -> list[list[str]]:
        return [c.argv for c in self.calls if _subsequence(tokens, c.argv)]


class FakeKeyring:
    def __init__(
        self,
        items: dict[tuple[str, str], str] | None = None,
        error: BaseException | None = None,
        journal: list[tuple[Any, ...]] | None = None,
    ) -> None:
        self.items = dict(items or {})
        self.error = error
        self.sets: list[tuple[str, str, str]] = []
        self.journal = journal if journal is not None else []

    def get_password(self, service: str, account: str) -> str | None:
        self.journal.append(("keyring.get", service, account))
        if self.error is not None:
            raise self.error
        return self.items.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self.journal.append(("keyring.set", service, account))
        self.sets.append((service, account, value))
        self.items[(service, account)] = value


class FakeProc:
    def __init__(self, ready: bool, journal: list[tuple[Any, ...]]) -> None:
        self.ready = ready
        self.pid = 4242
        self.terminated = False
        self.ready_timeouts: list[float] = []
        self.journal = journal

    def wait_ready(self, timeout: float) -> bool:
        self.ready_timeouts.append(timeout)
        self.journal.append(("ready", self.ready))
        return self.ready

    def terminate(self) -> None:
        self.terminated = True
        self.journal.append(("terminate",))


class FakeSpawner:
    def __init__(self, journal: list[tuple[Any, ...]], ready: bool = True) -> None:
        self.journal = journal
        self.ready = ready
        self.spawned: list[dict[str, Any]] = []
        self.procs: list[FakeProc] = []

    def spawn(self, argv: Any, *, env: Any, bundle: bytes) -> FakeProc:
        self.spawned.append(
            {"argv": [str(a) for a in argv], "env": None if env is None else dict(env), "bundle": bundle}
        )
        self.journal.append(("spawn",))
        proc = FakeProc(self.ready, self.journal)
        self.procs.append(proc)
        return proc


@dataclass
class FakeClock:
    now: float = 0.0
    slept: list[float] = field(default_factory=list)

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


@dataclass
class World:
    """One fake environment: the ctx under test plus the fakes behind it."""

    ctx: Any
    runner: FakeRunner
    keyring: FakeKeyring
    spawner: FakeSpawner
    clock: FakeClock
    journal: list[tuple[Any, ...]]
    kills: list[tuple[int, int]]
    home: Path

    @property
    def out(self) -> str:
        return str(self.ctx.out.getvalue())

    @property
    def err(self) -> str:
        return str(self.ctx.err.getvalue())


def make_world(
    tmp_path: Path,
    *,
    env: dict[str, str] | None = None,
    tty: bool = False,
    stdin_text: str = "",
    getpass_value: str = "",
    ready: bool = True,
    kill: Callable[[int, int], None] | None = None,
    answer: bool = False,
) -> World:
    from reader.cli.commands import Ctx

    journal: list[tuple[Any, ...]] = []
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    runner = FakeRunner(journal)
    keyring = FakeKeyring(journal=journal)
    spawner = FakeSpawner(journal, ready=ready)
    clock = FakeClock()
    kills: list[tuple[int, int]] = []

    def record_kill(pid: int, sig: int) -> None:
        kills.append((pid, sig))
        journal.append(("kill", sig))
        if kill is not None:
            kill(pid, sig)

    ctx = Ctx(
        runner=runner,
        keyring=keyring,
        env=env if env is not None else {"PATH": "/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8"},
        run_dir=tmp_path / "run",
        log_dir=tmp_path / "logs",
        compose_file=COMPOSE,
        spawner=spawner,
        out=StringIO(),
        err=StringIO(),
        stdin=StringIO(stdin_text),
        is_tty=tty,
        getpass=lambda _prompt: getpass_value,
        ask=lambda _prompt: answer,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        kill=record_kill,
    )
    return World(ctx, runner, keyring, spawner, clock, journal, kills, home)


def write_config(directory: Path, text: str | None = None) -> Path:
    """VEC-CLI-1 (or `text`) as reader.yaml in `directory`; returns the resolved path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "reader.yaml"
    path.write_text(
        text if text is not None else (FIXTURES / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return path.resolve()


def stack(
    world: World, model: dict[str, Any], *, volume_exists: bool = False, ps: bytes | None = None
) -> None:
    """Answer the docker calls of `reader up` for `model`."""
    world.runner.on("docker", "compose", "config", result=json.dumps(model).encode())
    world.runner.on("docker", "info", result=(T0 + "\n").encode())
    world.runner.on(
        "docker",
        "volume",
        "inspect",
        result=(0, b"[]", b"")
        if volume_exists
        else (1, b"", f"Error response from daemon: get {model['name']}_pgdata: no such volume".encode()),
    )
    world.runner.on("docker", "compose", "ps", result=ps if ps is not None else healthy(model))


class Lock:
    def __init__(self, fd: int) -> None:
        self.fd: int | None = fd

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


@contextmanager
def held_lock(run_dir: Path, pid: int | None = None) -> Iterator[Lock]:
    """Hold run/watcher.lock the way a running watcher does (flock, pid inside)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(run_dir / "watcher.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.ftruncate(fd, 0)
    os.write(fd, str(pid if pid is not None else os.getpid()).encode())
    lock = Lock(fd)
    try:
        yield lock
    finally:
        lock.release()


# ------------------------------------------------------------------------------------------------------------
# File-write audit (INV-CLI-1, INV-CLI-6): an audit hook installed once, active only inside `writes()`.

_WRITES: list[str] | None = None


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if _WRITES is None or event != "open":
        return
    path, mode, flags = args
    writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
        isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)
    )
    if writing and isinstance(path, (str, bytes, os.PathLike)) and "__pycache__" not in os.fsdecode(path):
        _WRITES.append(os.fsdecode(path))


sys.addaudithook(_audit)


@contextmanager
def writes() -> Iterator[list[str]]:
    global _WRITES
    _WRITES = []
    try:
        yield _WRITES
    finally:
        _WRITES = None
