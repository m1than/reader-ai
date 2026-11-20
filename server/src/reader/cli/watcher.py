"""The secret-delivery watcher (ADR-0026 option A; FR-CLI-6 to FR-CLI-9, FR-CLI-20).

Started by `reader up` as `python -m reader.cli.watcher`. It reads the values once from fd 3, holds them in
memory, subscribes to container start events, reconciles, writes one byte on fd 4, and delivers each value
atomically into the consumer's tmpfs over `docker exec -i` stdin on every start of a consumer.
"""

import argparse
import contextlib
import fcntl
import json
import logging
import os
import resource
import signal
import struct
import sys
import threading
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from reader.cli.errors import CliError
from reader.cli.log import LOGGER, log_event, open_watcher_log
from reader.cli.runner import (
    BUNDLE_FD,
    READY,
    READY_FD,
    CommandFailed,
    CommandTimeout,
    Runner,
    Stream,
    SubprocessRunner,
    safe_env,
)
from reader.cli.stack import DELIVERY_SCRIPT, NAME_PATTERN, Delivery
from reader.config import Secret

if TYPE_CHECKING:
    from reader.cli.commands import Ctx

EXEC_TIMEOUT: Final = 10  # s per docker exec (FR-CLI-6, structural)
RETRIES: Final = 3
RETRY_DELAY: Final = 1  # s
BACKOFF: Final = (1, 2, 4)  # s between event-stream resubscriptions (FR-CLI-8)
STOP_GRACE: Final = 15  # s before SIGKILL; longer than EXEC_TIMEOUT (FR-CLI-7)
STOP_POLL: Final = 0.25  # s
PROJECT: Final = "com.docker.compose.project"
ONEOFF: Final = "com.docker.compose.oneoff"
SERVICE: Final = "com.docker.compose.service"
_LEN: Final = struct.Struct(">I")

Job = Callable[[], None]


# Hand-off bundle (fd 3) -----------------------------------------------------------------------------------


def encode_bundle(values: Mapping[str, Secret]) -> bytes:
    parts: list[bytes] = []
    for name in sorted(values):
        key = name.encode("utf-8")
        value = values[name].reveal().encode("utf-8")
        parts += [_LEN.pack(len(key)), key, _LEN.pack(len(value)), value]
    return b"".join(parts)


def decode_bundle(data: bytes) -> dict[str, Secret]:
    values: dict[str, Secret] = {}
    view = memoryview(data)
    while view:
        fields: list[bytes] = []
        for _ in range(2):
            if len(view) < _LEN.size:
                raise ValueError("truncated bundle")
            (size,) = _LEN.unpack(view[: _LEN.size])
            view = view[_LEN.size :]
            if len(view) < size:
                raise ValueError("truncated bundle")
            fields.append(bytes(view[:size]))
            view = view[size:]
        name = fields[0].decode("ascii", "replace")
        if not NAME_PATTERN.fullmatch(name):
            raise ValueError("bad name in bundle")
        text: str | None
        try:
            text = fields[1].decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is None:
            raise ValueError("bundle value is not UTF-8")
        values[name] = Secret(text)
    return values


# Event trust (FR-CLI-9) -----------------------------------------------------------------------------------


def event_target(event: object, project: str, consumers: Collection[str]) -> tuple[str, str] | None:
    """(container id, service) when the event's own labels name a consumer of this project, else None."""
    if not isinstance(event, Mapping) or event.get("Type") != "container" or event.get("Action") != "start":
        return None
    actor = event.get("Actor")
    attributes = actor.get("Attributes") if isinstance(actor, Mapping) else None
    if not isinstance(actor, Mapping) or not isinstance(attributes, Mapping):
        return None
    container, service = actor.get("ID"), attributes.get(SERVICE)
    if attributes.get(PROJECT) != project or attributes.get(ONEOFF) != "False":
        return None
    if (
        not isinstance(container, str)
        or not container
        or not isinstance(service, str)
        or service not in consumers
    ):
        return None
    return container, service


def events_argv(project: str, since: str) -> list[str]:
    return [
        "docker",
        "events",
        "--since",
        since,
        "--filter",
        "type=container",
        "--filter",
        "event=start",
        "--filter",
        f"label={PROJECT}={project}",
        "--format",
        "{{json .}}",
    ]


class Watcher:
    def __init__(
        self,
        *,
        runner: Runner,
        env: Mapping[str, str],
        project: str,
        deliveries: Sequence[Delivery],
        values: Mapping[str, Secret],
        sleep: Callable[[float], None],
        submit: Callable[[Job], Any] | None = None,
    ) -> None:
        self.runner = runner
        self.env = dict(env)
        self.project = project
        self.values = dict(values)
        self.sleep = sleep
        self.by_service: dict[str, list[Delivery]] = {}
        for delivery in deliveries:
            self.by_service.setdefault(delivery.service, []).append(delivery)
        self._stopping = threading.Event()
        self._guard = threading.Lock()
        self._containers: dict[str, threading.Lock] = {}
        self._stream: Stream | None = None
        self._executor: ThreadPoolExecutor | None = None
        if submit is None:
            self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="deliver")
            executor = self._executor
            submit = executor.submit
        self.submit: Callable[[Job], Any] = submit

    # control

    def stop(self) -> None:
        """SIGTERM: no new delivery or retry starts; the event stream ends; in-flight calls finish."""
        self._stopping.set()
        stream = self._stream
        if stream is not None:
            stream.close()

    def shutdown(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=True)

    # delivery (FR-CLI-6)

    def _container_lock(self, container: str) -> threading.Lock:
        with self._guard:
            return self._containers.setdefault(container, threading.Lock())

    def deliver(self, container: str, service: str) -> None:
        """Every file of `service` into `container`, one after another (serial per container)."""
        with self._container_lock(container):
            for delivery in self.by_service.get(service, []):
                if self._stopping.is_set():
                    return
                self._deliver_one(container, delivery)

    def _deliver_one(self, container: str, delivery: Delivery) -> None:
        if not NAME_PATTERN.fullmatch(delivery.name):
            raise ValueError("delivery name outside the pattern")
        data = self.values[delivery.name].reveal().encode("utf-8")
        argv = [
            "docker",
            "exec",
            "-i",
            "-u",
            str(delivery.uid),
            container,
            "sh",
            "-c",
            DELIVERY_SCRIPT,
            "sh",
            delivery.name,
        ]
        outcome: object = None
        for attempt in range(1 + RETRIES):
            if attempt:
                if self._stopping.is_set():
                    return
                self.sleep(RETRY_DELAY)
            try:
                outcome = self.runner.run(argv, env=self.env, stdin=data, timeout=EXEC_TIMEOUT).code
            except CommandTimeout:
                outcome = "timeout"
            except CommandFailed:
                outcome = "not_started"
            if outcome == 0:
                log_event(
                    LOGGER,
                    "delivered",
                    container=container[:12],
                    service=delivery.service,
                    name=delivery.name,
                )
                return
        log_event(
            LOGGER,
            "delivery_failed",
            logging.ERROR,
            container=container[:12],
            service=delivery.service,
            name=delivery.name,
            outcome=outcome,
        )

    def _job(self, container: str, service: str) -> Job:
        def job() -> None:
            try:
                self.deliver(container, service)
            except Exception as error:  # a job must never die silently in the executor
                log_event(
                    LOGGER,
                    "delivery_error",
                    logging.ERROR,
                    container=container[:12],
                    service=service,
                    error=type(error).__name__,
                )

        return job

    # events and reconcile (FR-CLI-8, FR-CLI-9)

    def handle_line(self, line: str) -> None:
        if self._stopping.is_set():
            return
        try:
            event = json.loads(line)
        except ValueError:
            return
        target = event_target(event, self.project, self.by_service.keys())
        if target is None:
            if isinstance(event, Mapping) and event.get("Type") == "container":
                actor = event.get("Actor")
                container = actor.get("ID") if isinstance(actor, Mapping) else None
                log_event(LOGGER, "event_ignored", logging.DEBUG, container=str(container)[:12])
            return
        self.submit(self._job(*target))

    def reconcile(self) -> None:
        listed = self._quiet(
            ["docker", "ps", "-q", "--no-trunc", "--filter", f"label={PROJECT}={self.project}"]
        )
        ids = listed.split() if listed else []
        if not ids:
            return
        labels = self._quiet(["docker", "inspect", "--format", "{{.Id}} {{json .Config.Labels}}", *ids]) or ""
        for line in labels.splitlines():
            container, _, raw = line.partition(" ")
            try:
                attributes = json.loads(raw)
            except ValueError:
                continue
            self.handle_line(
                json.dumps(
                    {
                        "Type": "container",
                        "Action": "start",
                        "Actor": {"ID": container, "Attributes": attributes},
                    }
                )
            )

    def _quiet(self, argv: list[str]) -> str | None:
        try:
            result = self.runner.run(argv, env=self.env, timeout=EXEC_TIMEOUT)
        except CommandFailed, CommandTimeout:
            return None
        return result.out.decode("utf-8", "replace") if result.code == 0 else None

    def run(self, ready: Callable[[], None], since: str) -> int:
        """Subscribe, reconcile, signal readiness once, follow events; resubscribe; fail closed."""
        # The subscription replays every event since `since`, so no start before it is established is lost.
        failures = 0
        signalled = False
        while not self._stopping.is_set():
            try:
                stream: Stream | None = self.runner.stream(events_argv(self.project, since), env=self.env)
            except CommandFailed:
                stream = None
            if stream is not None:
                self._stream = stream
                self.reconcile()
                if not signalled:
                    ready()
                    signalled = True
                for line in stream:
                    failures = 0
                    self.handle_line(line)
            if self._stopping.is_set():
                break
            failures += 1
            if failures > len(BACKOFF):
                log_event(LOGGER, "events_failed", logging.ERROR, attempts=failures)
                return 1
            log_event(LOGGER, "events_resubscribe", logging.WARNING, attempt=failures)
            self.sleep(BACKOFF[failures - 1])
            since = (self._quiet(["docker", "info", "--format", "{{.SystemTime}}"]) or since).strip() or since
        return 0


# Lock, status and stop (FR-CLI-7) -------------------------------------------------------------------------


def acquire_lock(path: Path) -> int:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    held = False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        held = True
    if held:
        os.close(fd)
        raise CliError("watcher_running", "another watcher holds the lock")
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    return fd


def lock_holder(path: Path) -> int | None:
    """The recorded pid while the lock is held, else None. A held lock with an unreadable pid gives 0."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raw = os.read(fd, 32).strip()
            return int(raw) if raw.isdigit() else 0
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


def stop_watcher(ctx: Ctx) -> bool:
    """Signal the recorded pid only while its lock is held: SIGTERM, SIGKILL after STOP_GRACE."""
    lock = ctx.run_dir / "watcher.lock"
    pid = lock_holder(lock)
    if pid is None or pid <= 0:
        return False
    try:
        ctx.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = ctx.monotonic() + STOP_GRACE
    while lock_holder(lock) is not None:
        if ctx.monotonic() >= deadline:
            with contextlib.suppress(ProcessLookupError):
                ctx.kill(pid, signal.SIGKILL)
            break
        ctx.sleep(STOP_POLL)
    return True


# Process entry -------------------------------------------------------------------------------------------


def harden(run_dir: Path) -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.umask(0o077)
    os.chdir(run_dir)


def _read_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    while chunk := os.read(fd, 65536):
        chunks.append(chunk)
    os.close(fd)
    return b"".join(chunks)


def _signal_ready() -> None:
    os.write(READY_FD, READY)
    os.close(READY_FD)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reader.cli.watcher")
    for flag in ("--project", "--since", "--plan", "--run-dir", "--log-dir"):
        parser.add_argument(flag, required=True)
    args = parser.parse_args(argv)
    run_dir, log_dir = Path(args.run_dir), Path(args.log_dir)
    harden(run_dir)
    values = decode_bundle(_read_all(BUNDLE_FD))
    lock_fd = acquire_lock(run_dir / "watcher.lock")
    LOGGER.addHandler(open_watcher_log(log_dir))
    LOGGER.setLevel(logging.INFO)
    deliveries = [Delivery(**entry) for entry in json.loads(args.plan)]
    watcher = Watcher(
        runner=SubprocessRunner(),
        env=safe_env(os.environ),
        project=args.project,
        deliveries=deliveries,
        values=values,
        sleep=time.sleep,
    )
    signal.signal(signal.SIGTERM, lambda _signum, _frame: watcher.stop())
    log_event(
        LOGGER,
        "watcher_started",
        project=args.project,
        files=len(values),
        plan=json.dumps([asdict(d) for d in deliveries]),
    )
    code = watcher.run(ready=_signal_ready, since=args.since)
    watcher.shutdown()
    log_event(LOGGER, "watcher_stopped", code=code)
    os.close(lock_fd)
    return code


if __name__ == "__main__":
    sys.exit(main())
