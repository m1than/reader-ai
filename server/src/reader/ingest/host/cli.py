"""`reader-ingest`: the host command that converts a declared PDF and hands the artifact to the worker.

    reader-ingest MATERIAL_ID [--config PATH]            convert, then receive over docker exec stdin
    reader-ingest MATERIAL_ID --requeue [--config PATH]  requeue a dead import without converting
    reader-ingest --setup [--config PATH]                converter venv and models (the one networked step)

SRV-INGEST FR-INGEST-1..5, -10, -11 (host mapping), -12, -16; ADR-0027. Exit codes: 0 done or nothing to do,
2 configuration or setup, 3 busy or stack unhealthy, 4 import dead, 5 conversion or receive failed. Errors are
one line `reader-ingest: <code>`; nothing the child prints, and no material text, reaches the terminal.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol, TextIO

from reader.config import ConfigError, load_config
from reader.ingest import artifact, tarstream
from reader.ingest.artifact import ArtifactError
from reader.ingest.host import records, sandbox, setup
from reader.ingest.host.mapping import parse_progress, render_code_line

CHILD_TIMEOUT_S: Final = 4 * 3600.0  # ADR-0021's 10 pages/min gives 94 min for the pilot (spec header)
CHILD_GRACE_S: Final = 10.0
STATUS_TIMEOUT_S: Final = 60.0
RECEIVE_TIMEOUT_S: Final = 330.0  # receive ends itself at 300 s
DOCKER_TIMEOUT_S: Final = 30.0
LINE_LIMIT: Final = 4096  # bytes per child stdout line; a progress line is under 60
ENV_ALLOWED: Final = ("PATH", "HOME", "LANG", "DOCKER_HOST")
LABELS: Final = {
    "com.docker.compose.service": "worker",
    "com.docker.compose.oneoff": "False",
}


class Runner(Protocol):
    def run(
        self, argv: list[str], env: dict[str, str], timeout: float, stdin: bytes | None = None
    ) -> tuple[int, bytes]: ...


class Child(Protocol):
    @property
    def pid(self) -> int: ...

    def stdout_lines(self) -> Iterator[bytes]: ...
    def wait(self, timeout: float | None = None) -> int: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...


Spawn = Callable[[list[str], dict[str, str], Path], Child]


class Stop(Exception):
    def __init__(self, code: str, exit_code: int, detail: str | None = None) -> None:
        super().__init__(code)
        self.code, self.exit_code, self.detail = code, exit_code, detail


class SystemRunner:
    def run(
        self, argv: list[str], env: dict[str, str], timeout: float, stdin: bytes | None = None
    ) -> tuple[int, bytes]:
        try:
            done = subprocess.run(  # noqa: S603 (fixed argv built here)
                argv, env=env, input=stdin, capture_output=True, timeout=timeout, check=False
            )
        except (subprocess.TimeoutExpired, OSError):
            return -1, b""  # never rendered: the argv may name a container, the exception the command
        return done.returncode, done.stdout


@dataclass
class SystemChild:
    process: subprocess.Popen[bytes]
    stderr_bytes: int = 0

    @property
    def pid(self) -> int:
        return self.process.pid

    def stdout_lines(self) -> Iterator[bytes]:
        assert self.process.stdout is not None  # noqa: S101
        while line := self.process.stdout.readline(LINE_LIMIT):  # a newline-free flood stays bounded
            yield line

    def wait(self, timeout: float | None = None) -> int:
        return self.process.wait(timeout)

    def terminate(self) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.process.pid, signal.SIGTERM)

    def kill(self) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.process.pid, signal.SIGKILL)


def system_spawn(argv: list[str], env: dict[str, str], cwd: Path) -> SystemChild:
    process = subprocess.Popen(  # noqa: S603 (the sandbox-exec argv of FR-INGEST-4)
        argv,
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    child = SystemChild(process)

    def drain() -> None:  # stderr is counted, never forwarded (FR-INGEST-5)
        assert process.stderr is not None  # noqa: S101
        for chunk in iter(lambda: process.stderr.read(65536) if process.stderr else b"", b""):
            child.stderr_bytes += len(chunk)

    threading.Thread(target=drain, daemon=True).start()
    return child


@dataclass
class Deps:
    runner: Runner
    spawn: Spawn
    probe: records.Probe
    check_setup: Callable[[Path], str | None] = setup.check_setup
    venv: Path | None = None
    pyroot: Path | None = None
    models: Path | None = None
    cpus: int = field(default_factory=lambda: os.cpu_count() or 1)
    start_time: Callable[[int], str | None] = field(default_factory=lambda: records.SystemProbe().start_time)


def real_deps(data_dir: Path | None = None) -> Deps:
    deps = Deps(runner=SystemRunner(), spawn=system_spawn, probe=records.SystemProbe())
    if data_dir is not None:
        resolve_paths(deps, data_dir)
    return deps


def resolve_paths(deps: Deps, data_dir: Path) -> None:
    deps.venv = deps.venv or setup.venv_dir(data_dir)
    deps.models = deps.models or setup.models_dir(data_dir)
    deps.pyroot = deps.pyroot or setup.pyroot_of(deps.venv)


def _env() -> dict[str, str]:
    """FR-INGEST-2: the allow-listed environment of every subprocess but the child (FR-INGEST-4's own)."""
    return {k: os.environ[k] for k in ENV_ALLOWED if k in os.environ}


def _printer(out: TextIO) -> Callable[[str], None]:
    def write(line: str) -> None:
        out.write(line + "\n")
        out.flush()

    return write


def _json_line(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


# ---- conversion (FR-INGEST-4, -5, -10) ---------------------------------------------------------------------


def convert_members(
    work: Path,
    deps: Deps,
    report: Callable[[str], None],
    on_start: Callable[[Child], None] | None = None,
) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Run the confined child on `<work>/in/material.pdf`; returns the members, read once and validated, and
    the manifest the validator returned. These bytes, never WORK again, are what is sent (FR-INGEST-10)."""
    if deps.venv is None or deps.pyroot is None or deps.models is None:
        raise Stop("setup_missing", 2)
    for sub in ("home", "tmp"):
        (work / sub).mkdir(mode=0o700, exist_ok=True)
    argv = sandbox.child_argv(deps.venv, deps.pyroot, deps.models, work)
    child: Child | None = None
    timer: threading.Timer | None = None
    try:
        child = deps.spawn(argv, sandbox.child_env(work, deps.cpus), Path(os.path.realpath(work)))
        if on_start is not None:
            on_start(child)
        timer = threading.Timer(CHILD_TIMEOUT_S, child.kill)
        timer.daemon = True
        timer.start()
        for line in child.stdout_lines():
            parsed = parse_progress(line.strip())
            rendered = f"page {parsed[0]}/{parsed[1]}" if parsed else render_code_line(line.strip())
            if rendered is not None:
                report(rendered)
        code = child.wait()
    except BaseException:
        if child is not None:
            child.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.wait(CHILD_GRACE_S)
        raise
    finally:
        if timer is not None:
            timer.cancel()
        if child is not None:
            child.kill()  # the whole session, so nothing the child started still writes into WORK
    if code != 0:
        raise Stop("convert_failed", 5)
    try:
        members = artifact.read_members(work / "out")
        meta = artifact.validate_members(members)
    except ArtifactError as error:
        raise Stop("convert_failed", 5, error.code) from None
    return members, meta


def convert(
    work: Path,
    deps: Deps,
    report: Callable[[str], None],
    on_start: Callable[[Child], None] | None = None,
) -> Path:
    """convert_members, returning `<work>/out` (for the [host] tests, which read the files back)."""
    convert_members(work, deps, report, on_start)
    return work / "out"


# ---- the stack (FR-INGEST-2) -------------------------------------------------------------------------------


def _reader_binary() -> str:
    sibling = Path(sys.executable).with_name("reader")
    return str(sibling) if sibling.exists() else "reader"


def worker_container(deps: Deps) -> str:
    code, raw = deps.runner.run([_reader_binary(), "status", "--json"], _env(), DOCKER_TIMEOUT_S)
    status = _json_line(raw) if code == 0 else None
    if not isinstance(status, dict) or not isinstance(status.get("project"), str):
        raise Stop("stack_unhealthy", 3)
    project = status["project"]
    labels = {"com.docker.compose.project": project, **LABELS}
    argv = ["docker", "ps", "-q", "--no-trunc"]
    for key, value in labels.items():
        argv += ["--filter", f"label={key}={value}"]
    code, raw = deps.runner.run(argv, _env(), DOCKER_TIMEOUT_S)
    ids = raw.decode("ascii", "replace").split() if code == 0 else []
    if len(ids) != 1:
        raise Stop("stack_unhealthy", 3)
    code, raw = deps.runner.run(["docker", "inspect", ids[0]], _env(), DOCKER_TIMEOUT_S)
    inspected = _json_line(raw.replace(b"\n", b" ")) if code == 0 else None
    ok = (
        isinstance(inspected, list)
        and len(inspected) == 1
        and inspected[0].get("State", {}).get("Running") is True
        and all((inspected[0].get("Config", {}).get("Labels") or {}).get(k) == v for k, v in labels.items())
    )
    if not ok:
        raise Stop("stack_unhealthy", 3)
    return ids[0]


def _exec(
    deps: Deps, container: str, module: str, args: list[str], timeout: float, stdin: bytes | None = None
) -> tuple[int, Any]:
    argv = ["docker", "exec", "-i", container, "python", "-I", "-m", module, *args]
    code, raw = deps.runner.run(argv, _env(), timeout, stdin)
    return code, _json_line(raw)


# ---- the command (FR-INGEST-1) -----------------------------------------------------------------------------


@dataclass
class Material:
    id: str
    path: Path
    sha256: str
    data_dir: Path


def _material(material_id: str, config_path: Path) -> Material:
    try:
        config = load_config(config_path)
    except ConfigError as error:
        raise Stop("config_invalid", 2, str(error)) from None
    base = config_path.resolve().parent
    data_dir = Path(config.instance.data_dir)
    data_dir = data_dir if data_dir.is_absolute() else base / data_dir
    for entry in config.materials:
        if entry.id == material_id:
            file = Path(entry.file)
            return Material(entry.id, file if file.is_absolute() else base / file, entry.sha256, data_dir)
    raise Stop("unknown_material", 2)


def _copy(material: Material, target: Path) -> None:
    """FR-INGEST-1: open without following links, require a regular file, hash the copied bytes."""
    try:
        fd = os.open(material.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise Stop("material_unreadable", 2) from None
    with os.fdopen(fd, "rb") as source:
        import stat as stat_mod

        if not stat_mod.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise Stop("material_not_a_file", 2)
        target.parent.mkdir(mode=0o700, exist_ok=True)
        digest = hashlib.sha256()
        out_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(out_fd, "wb") as copy:
            for chunk in iter(lambda: source.read(1 << 20), b""):
                digest.update(chunk)
                copy.write(chunk)
        os.chmod(target, 0o400)
    if digest.hexdigest() != material.sha256:
        raise Stop("sha256_mismatch", 2)


@contextlib.contextmanager
def _material_lock(data_dir: Path, sha256: str) -> Iterator[None]:
    locks = data_dir / "ingest" / "locks"
    locks.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(locks / f"{sha256}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Stop("busy", 3) from None
        yield
    finally:
        os.close(fd)


CONVERT_LOCK: Final = "convert.lock"


@contextlib.contextmanager
def convert_lock(data_dir: Path) -> Iterator[int]:
    """One converter run on the host at a time (FR-INGEST-1; ADR-0029 amendment). Yields the lock's fd.

    The fd is O_CLOEXEC, so no child holds the lock: a crashed holder releases it with its own exit.
    """
    locks = data_dir / "ingest" / "locks"
    locks.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(locks / CONVERT_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Stop("busy", 3, "another conversion is running; try again when it ends") from None
        yield fd
    finally:
        os.close(fd)


def _status(deps: Deps, container: str, sha256: str, out: TextIO) -> str:
    code, reply = _exec(
        deps, container, "reader.ingest.status", ["--material-sha256", sha256], STATUS_TIMEOUT_S
    )
    if code != 0 or not isinstance(reply, dict) or not isinstance(reply.get("status"), str):
        raise Stop("stack_unhealthy", 3)
    status = str(reply["status"])
    if status == "succeeded":
        out.write(f"already ingested (converter {reply.get('converter_version')})\n")
    elif status in ("queued", "running"):
        out.write("import in progress\n")
    elif status == "dead":
        raise Stop("import_dead", 4, "run again with --requeue")
    return status


def _receive(deps: Deps, container: str, members: dict[str, bytes], out: TextIO) -> int:
    buffer = io.BytesIO()
    tarstream.write_members(members, buffer)  # the validated bytes, not a second read of WORK
    code, reply = _exec(deps, container, "reader.ingest.receive", [], RECEIVE_TIMEOUT_S, buffer.getvalue())
    if isinstance(reply, dict) and isinstance(reply.get("error"), str):
        raise Stop(str(reply["error"]), 5)
    if code != 0 or not isinstance(reply, dict) or reply.get("result") not in (
        "queued",
        "already_queued",
        "already_done",
        "dead",
    ):
        raise Stop("receive_failed", 5)
    if reply["result"] == "dead":
        raise Stop("import_dead", 4, "run again with --requeue")
    out.write(f"import {reply['result']} (artifact {str(reply.get('artifact_id', ''))[:12]})\n")
    return 0


def _ingest(material: Material, deps: Deps, requeue: bool, out: TextIO) -> int:
    ingest_root = material.data_dir / "ingest"
    run_dir, work_root = ingest_root / "run", ingest_root / "work"
    with _material_lock(material.data_dir, material.sha256):
        if requeue:  # starts no converter: no convert.lock, and only this material's records are cleaned
            records.cleanup_stale(run_dir, work_root, material.sha256, deps.probe)
            container = worker_container(deps)
            args = ["--material-sha256", material.sha256]
            code, reply = _exec(deps, container, "reader.ingest.requeue", args, STATUS_TIMEOUT_S)
            if code != 0 or not isinstance(reply, dict) or "result" not in reply:
                raise Stop("requeue_failed", 5)
            out.write(f"requeue: {reply['result']}\n")
            return 0
        with convert_lock(material.data_dir):
            # a free convert.lock proves that no live run owns any record, of any material
            records.cleanup_stale(run_dir, work_root, None, deps.probe)
            return _convert_run(material, deps, run_dir, work_root, out)


def _convert_run(material: Material, deps: Deps, run_dir: Path, work_root: Path, out: TextIO) -> int:
    problem = deps.check_setup(material.data_dir)
    if problem is not None:
        raise Stop(problem, 2, "run `reader-ingest --setup`")
    resolve_paths(deps, material.data_dir)
    name = records.new_run(run_dir, work_root, material.sha256)
    work = work_root / name
    try:
        _copy(material, work / "in" / "material.pdf")
        container = worker_container(deps)
        if _status(deps, container, material.sha256, out) != "none":
            return 0

        def record_child(child: Child) -> None:
            records.set_child(run_dir, name, child.pid, deps.start_time(child.pid) or "")

        members, meta = convert_members(work, deps, _printer(out), record_child)
        if meta["material_sha256"] != material.sha256:
            raise Stop("convert_failed", 5)
        return _receive(deps, container, members, out)
    finally:
        records.end_run(run_dir, work_root, name)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reader-ingest")
    parser.add_argument("material", nargs="?")
    parser.add_argument("--config", default="reader.yaml", type=Path)
    parser.add_argument("--requeue", action="store_true")
    parser.add_argument("--setup", action="store_true")
    return parser


def _terminate(signum: int, _: Any) -> None:
    raise Stop("interrupted", 130 if signum == signal.SIGINT else 143)


def ingest(argv: list[str], deps: Deps, out: TextIO, err: TextIO) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.setup:
            material_cfg = load_config(args.config)
            base = args.config.resolve().parent
            data_dir = Path(material_cfg.instance.data_dir)
            data_dir = data_dir if data_dir.is_absolute() else base / data_dir
            problem = setup.run_setup(data_dir, _env(), _printer(out))
            if problem is not None:
                raise Stop(problem, 2)
            return 0
        if not args.material:
            raise Stop("material_missing", 2)
        material = _material(args.material, args.config)
        return _ingest(material, deps, args.requeue, out)
    except Stop as stop:
        err.write(f"reader-ingest: {stop.code}" + (f" ({stop.detail})" if stop.detail else "") + "\n")
        return stop.exit_code
    except ConfigError as error:
        err.write(f"reader-ingest: config_invalid ({error})\n")
        return 2


def main(argv: list[str] | None = None) -> int:
    previous = {s: signal.signal(s, _terminate) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        return ingest(sys.argv[1:] if argv is None else argv, real_deps(), sys.stdout, sys.stderr)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    sys.exit(main())
