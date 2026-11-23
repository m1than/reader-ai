"""Bounded `docker exec` for watch mode (SRV-INGEST FR-INGEST-21, INV-INGEST-10).

stdout is read through a bound (4,097 bytes for a reply, `size` + 1 for `fetch`); reaching it kills the exec
client and is reported as `overflow`. stderr is drained concurrently into a byte counter and never stored. A
host-side timeout kills the exec client. The exit code is checked before any byte is used (`reply`).
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Protocol

REPLY_LIMIT: Final = 4096
CANCEL: Final = b'{"cancel":true}\n'
READ: Final = 1 << 16


@dataclass(frozen=True)
class Exec:
    code: int | None  # None when the client was killed (overflow or timeout)
    out: bytes  # empty when a sink received the bytes
    total: int  # stdout bytes read, at most limit + 1
    stderr_bytes: int
    overflow: bool
    timed_out: bool


class BoundedRunner(Protocol):
    def run(
        self,
        argv: list[str],
        env: dict[str, str],
        timeout: float,
        limit: int,
        stdin: bytes | None = None,
        sink: Callable[[bytes], None] | None = None,
    ) -> Exec: ...


class SystemRunner:
    """The real runner: argv as a list (never a shell), the allow-listed env, no inherited descriptors."""

    def run(
        self,
        argv: list[str],
        env: dict[str, str],
        timeout: float,
        limit: int,
        stdin: bytes | None = None,
        sink: Callable[[bytes], None] | None = None,
    ) -> Exec:
        process = subprocess.Popen(  # noqa: S603 (argv built from validated values)
            argv,
            env=env,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            start_new_session=True,  # its own group: a kill reaches anything holding the pipes
        )
        assert process.stdout is not None  # noqa: S101
        assert process.stderr is not None  # noqa: S101
        counted = [0]
        killed = threading.Event()

        def kill() -> None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)

        def drain() -> None:
            assert process.stderr is not None  # noqa: S101
            while chunk := process.stderr.read(READ):
                counted[0] += len(chunk)

        def feed() -> None:
            assert process.stdin is not None  # noqa: S101
            try:
                process.stdin.write(stdin or b"")
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        def expire() -> None:
            killed.set()
            kill()

        drainer = threading.Thread(target=drain, daemon=True)
        drainer.start()
        if stdin is not None:
            threading.Thread(target=feed, daemon=True).start()
        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()
        buffer = bytearray()
        total, overflow = 0, False
        try:
            while chunk := os.read(process.stdout.fileno(), READ):
                room = limit + 1 - total
                piece = chunk[:room]
                total += len(piece)
                if sink is not None:
                    sink(bytes(piece))
                else:
                    buffer += piece
                if total > limit:
                    overflow = True
                    kill()
                    break
        except BaseException:  # an interrupt (the watcher's SIGTERM) never waits for the exec client
            kill()
            raise
        finally:
            code = process.wait()
            timer.cancel()
            drainer.join(5)
            process.stdout.close()
        timed_out = killed.is_set()
        return Exec(
            code=None if overflow or timed_out else code,
            out=bytes(buffer),
            total=total,
            stderr_bytes=counted[0],
            overflow=overflow,
            timed_out=timed_out,
        )


Shape = Callable[[Any], bool]


def reply(result: Exec, shape: Shape) -> tuple[str, Any]:
    """FR-INGEST-20's framing, exit code first: ("ok", obj) | ("cancel", None) | ("error", code) |
    ("timeout", None) | ("bad_reply", None)."""
    if result.timed_out:
        return "timeout", None
    if result.overflow or result.code is None:
        return "bad_reply", None
    raw = result.out
    if raw.count(b"\n") != 1 or not raw.endswith(b"\n") or len(raw) > REPLY_LIMIT:
        return "bad_reply", None
    if result.code == 3:
        return ("cancel", None) if raw == CANCEL else ("bad_reply", None)
    try:
        obj = json.loads(raw)
    except ValueError:
        return "bad_reply", None
    if result.code == 0:
        good = isinstance(obj, dict) and "cancel" not in obj and shape(obj)
        return ("ok", obj) if good else ("bad_reply", None)
    if result.code == 1:
        ok = isinstance(obj, dict) and set(obj) == {"error"} and isinstance(obj["error"], str)
        return ("error", obj["error"]) if ok else ("bad_reply", None)
    return "bad_reply", None
