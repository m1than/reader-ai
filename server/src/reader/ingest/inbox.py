"""`python -I -m reader.ingest.inbox next|fetch|progress|fail …` in the worker container (FR-INGEST-20).

Framing, one shape per outcome: success is exit 0 and one JSON line of at most 4,096 bytes (for `fetch`,
exactly the row's `size` bytes); cancel is exit 3 and exactly `{"cancel":true}`; an error is exit 1 and one
line `{"error":"<code>"}`. `fetch` decides every refusal before its first byte. Arguments are validated
before any database access. Nothing is written to stderr that the host would read.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import stat
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, BinaryIO, Final

from sqlalchemy.ext.asyncio import AsyncEngine

import reader.ingest as ingest
from reader.ingest import params, uploads
from reader.ingest.errors import Cancel, IngestError

log = logging.getLogger("reader.ingest")

FAIL_CODES: Final = params.FAIL_CODES
CANCEL_LINE: Final = b'{"cancel":true}\n'
PHASES: Final = ("fetch", "load", "convert", "sending", "receive")
CLAIM: Final = re.compile(r"[0-9a-f]{32}")
SHA: Final = re.compile(r"[0-9a-f]{64}")
VERSION: Final = re.compile(r"[A-Za-z0-9._+-]{1,64}")
DEADLINES: Final = {"fetch": 300.0, "progress": 15.0, "fail": 15.0}


class ArgsError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # never print usage: the host reads stdout only
        raise ArgsError(message)


def _int_in(low: int, high: int) -> Any:
    def parse(raw: str) -> int:
        if not raw.isascii() or not raw.isdecimal() or not low <= int(raw) <= high:
            raise ArgsError(raw)
        return int(raw)

    return parse


def _match(pattern: re.Pattern[str]) -> Any:
    def parse(raw: str) -> str:
        if not pattern.fullmatch(raw):
            raise ArgsError("pattern")
        return raw

    return parse


def _uuid(raw: str) -> str:
    try:
        value = uuid.UUID(raw)
    except ValueError:
        raise ArgsError("upload id") from None
    if str(value) != raw:
        raise ArgsError("upload id")
    return raw


def _parser() -> _Parser:
    parser = _Parser(prog="reader.ingest.inbox", add_help=False, exit_on_error=False)
    sub = parser.add_subparsers(dest="cmd", required=True)
    nxt = sub.add_parser("next", add_help=False, exit_on_error=False)
    mode = nxt.add_mutually_exclusive_group(required=True)
    mode.add_argument("--peek", action="store_true")
    mode.add_argument("--claim", action="store_true")
    nxt.add_argument("--wait", type=_int_in(1, params.WAIT_MAX_S), required=True)
    nxt.add_argument("--host-state", choices=("ready", "setup_missing"), required=True)
    nxt.add_argument("--strays", type=_int_in(0, params.STRAYS_MAX), required=True)
    nxt.add_argument("--converter-version", type=_match(VERSION), required=True)
    nxt.add_argument("--watcher-sha", type=_match(SHA), required=True)
    nxt.add_argument("--busy", choices=("none", "claim", "cli_running"), required=True)
    for name in ("fetch", "progress", "fail"):
        cmd = sub.add_parser(name, add_help=False, exit_on_error=False)
        cmd.add_argument("--upload-id", type=_uuid, required=True)
        cmd.add_argument("--claim", type=_match(CLAIM), required=True)
        if name == "progress":
            cmd.add_argument("--pages-done", type=_int_in(0, 5000))
            cmd.add_argument("--pages-total", type=_int_in(0, 5000))
            cmd.add_argument("--phase", choices=PHASES)
        if name == "fail":
            cmd.add_argument("--code", choices=FAIL_CODES, required=True)
    return parser


def parse(argv: list[str]) -> argparse.Namespace:
    try:
        args = _parser().parse_args(argv)
    except (ArgsError, argparse.ArgumentError, ValueError, TypeError):
        raise ArgsError("arguments") from None
    if args.cmd == "progress":
        pages = args.pages_done is not None or args.pages_total is not None
        if pages == (args.phase is not None):
            raise ArgsError("arguments")
        done, total = args.pages_done, args.pages_total
        if pages and (done is None or total is None or done > total):
            raise ArgsError("arguments")
    return args


def _line(out: BinaryIO, payload: dict[str, Any]) -> None:
    raw = json.dumps(payload, separators=(",", ":")).encode() + b"\n"
    if len(raw) > params.REPLY_MAX:
        raise IngestError("reply_too_large")
    out.write(raw)
    out.flush()


async def _peek(engine: AsyncEngine, seconds: float) -> int:
    """LISTEN first, then count; while none is queued, wait for a notification for up to `seconds`."""
    notified = asyncio.Event()

    def listener(*_: Any) -> None:
        notified.set()

    async with engine.connect() as conn:
        raw = await conn.get_raw_connection()
        driver: Any = raw.driver_connection
        await driver.add_listener(params.NOTIFY_CHANNEL, listener)
        try:
            count = await uploads.queued_count(conn)
            await conn.commit()  # notifications arrive between transactions
            if count == 0:
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(seconds):
                        await notified.wait()
                count = await uploads.queued_count(conn)
                await conn.commit()
            return count
        finally:
            await driver.remove_listener(params.NOTIFY_CHANNEL, listener)


async def _next(engine: AsyncEngine, args: argparse.Namespace) -> dict[str, Any]:
    await uploads.expire(engine)
    host = {
        "state": args.host_state,
        "strays": args.strays,
        "version": args.converter_version,
        "sha": args.watcher_sha,
        "busy": args.busy,
    }
    async with engine.begin() as conn:
        await uploads.host_seen(conn, host)
    if args.claim:
        if args.host_state != "ready" or args.busy != "none":
            return {"upload": None}
        async with engine.begin() as conn:
            return {"upload": await uploads.claim_next(conn)}
    return {"queued": await _peek(engine, float(args.wait))}


def _copy_out(path: Path, size: int, out: BinaryIO) -> None:
    """The byte copy: the only read of an inbox file besides the upload's magic check (INV-INGEST-8)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise IngestError("missing") from None
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            raise IngestError("missing")
        sent = 0
        while chunk := source.read(params.CHUNK):
            out.write(chunk)
            sent += len(chunk)
        out.flush()
        if sent != size:
            raise OSError("short read")


async def _fetch(engine: AsyncEngine, blobs_root: Path, args: argparse.Namespace, out: BinaryIO) -> int:
    async with engine.begin() as conn:
        row = await uploads.under_claim(conn, args.upload_id, args.claim, states=("claimed", "converting"))
    if row is None:
        raise Cancel
    path = blobs_root / params.INBOX / f"{row.sha256}.pdf"
    try:
        await asyncio.to_thread(_copy_out, path, row.size, out)
    except IngestError:
        raise
    except OSError:
        log.warning("fetch_io_error id=%s", row.id)
        return 1  # after the first byte nothing more is written; the host checks the exit code first
    log.info("fetch_sent id=%s size=%d", row.id, row.size)
    return 0


async def _work(argv: list[str], engine: AsyncEngine, blobs_root: Path, out: BinaryIO) -> int:
    args = parse(argv)
    if args.cmd == "next":
        async with asyncio.timeout(args.wait + 10):
            _line(out, await _next(engine, args))
        return 0
    async with asyncio.timeout(DEADLINES[args.cmd]):
        if args.cmd == "fetch":
            return await _fetch(engine, blobs_root, args, out)
        async with engine.begin() as conn:
            if args.cmd == "progress":
                pages = None if args.phase else (args.pages_done, args.pages_total)
                live = await uploads.progress(conn, args.upload_id, args.claim, pages, args.phase)
            else:
                live = await uploads.fail(conn, args.upload_id, args.claim, args.code)
        if not live:  # after the commit: an expiry or a `received` mark written on the way is kept
            raise Cancel
        _line(out, {"ok": True})
        return 0


async def execute(argv: list[str], *, engine: AsyncEngine | None, blobs_root: Path, out: BinaryIO) -> int:
    """One command: its exit code, with exactly one framed reply (or the fetched bytes) on `out`."""
    try:
        if engine is None:
            parse(argv)  # tests: arguments are refused before any database access
            raise ArgsError("no engine")
        return await _work(argv, engine, blobs_root, out)
    except ArgsError:
        out.write(b'{"error":"arguments"}\n')
    except Cancel:
        out.write(CANCEL_LINE)
        return 3
    except IngestError as refusal:
        out.write(json.dumps({"error": refusal.code}, separators=(",", ":")).encode() + b"\n")
    except TimeoutError:
        out.write(b'{"error":"timeout"}\n')
    except Exception:  # driver detail is never printed
        out.write(b'{"error":"unavailable"}\n')
    out.flush()
    return 1


@contextlib.asynccontextmanager
async def _engine() -> AsyncIterator[AsyncEngine]:
    from reader.db import make_engine
    from reader.serve.main import Env, Failure, _target

    try:
        target = _target(Env.read(os.environ))
    except Failure:
        raise IngestError("runtime_unavailable") from None
    engine = make_engine(target)
    try:
        yield engine
    finally:
        await engine.dispose()


def _alarm(_: int, __: Any) -> None:
    raise TimeoutError


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    out = sys.stdout.buffer
    deadline = 335.0  # SIGALRM backstop above every command's own deadline (fetch 300 s, next ≤ 35 s)
    signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, deadline)

    async def run() -> int:
        try:
            parse(argv)
        except ArgsError:
            return await execute(argv, engine=None, blobs_root=ingest.BLOBS_DIR, out=out)
        try:
            async with _engine() as engine:
                return await execute(argv, engine=engine, blobs_root=ingest.BLOBS_DIR, out=out)
        except IngestError as error:
            out.write(json.dumps({"error": error.code}, separators=(",", ":")).encode() + b"\n")
            return 1

    try:
        return asyncio.run(run())
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == "__main__":
    sys.exit(main())
