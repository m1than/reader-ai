"""`python -I -m reader.ingest.receive`: validate a tar from stdin, store it, enqueue it (FR-INGEST-11).

Phases: read and validate, store (the final rename), enqueue. A rejection, or the 300 s deadline before
storage completes, leaves nothing under `<blobs>/ingest/` but this run's own temporary directory, which it
removes. After storage the content-addressed directory is valid and stays, whatever happens next
(`enqueue_failed`).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import re
import secrets
import shutil
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO, Final, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

import reader.ingest as ingest
from reader.config import ReaderConfig
from reader.ingest.artifact import ArtifactError, artifact_id, read_members, validate_dir, validate_members
from reader.ingest.errors import Cancel, IngestError
from reader.ingest.jobs import KIND
from reader.ingest.tarstream import read_tar

DEADLINE_S: Final = 300.0
log = logging.getLogger("reader.ingest")
UUID_RE: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
CLAIM_RE: Final = re.compile(r"[0-9a-f]{32}")


class Enqueuer(Protocol):
    async def enqueue(self, kind_name: str, args: Any, schedule_in: int = 0) -> Any: ...


class DeadlineExceeded(BaseException):
    """Raised by the deadline alarm inside whatever blocking call is running."""


def _fsync(path: Path, directory: bool = False) -> None:
    fd = os.open(path, os.O_RDONLY | (os.O_DIRECTORY if directory else 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _store(tmp: Path, root: Path, artifact: str) -> None:
    for name in ("manifest.json", "spans.jsonl", "outline.json"):
        _fsync(tmp / name)
    _fsync(tmp, directory=True)
    target = root / artifact
    if target.exists():
        try:
            validate_dir(target)
            shutil.rmtree(tmp)
            return
        except ArtifactError:
            aside = root / f".bad-{secrets.token_hex(8)}"
            os.rename(target, aside)
            os.rename(tmp, target)
            shutil.rmtree(aside, ignore_errors=True)
    else:
        os.rename(tmp, target)
    _fsync(root, directory=True)


async def receive(
    stream: BinaryIO,
    *,
    config: ReaderConfig,
    engine: AsyncEngine,
    jobs: Enqueuer,
    blobs_root: Path,
    deadline: float | None = None,
    state: dict[str, bool] | None = None,
    after_read: Callable[[], None] | None = None,
    upload_id: str | None = None,
    claim: str | None = None,
    before_mark: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """`state["stored"]` turns True once the final rename is done, for callers that see the deadline fire.

    With `upload_id` and `claim` (FR-INGEST-23) the material is that upload: the claim is checked (read only)
    before storage, and after the enqueue the row is marked `received`. Steps are idempotent, so a crash
    between enqueue and mark is repaired by the key-authority rule (FR-INGEST-19)."""
    deadline = time.monotonic() + DEADLINE_S if deadline is None else deadline
    state = {} if state is None else state
    state["stored"] = False

    def remaining() -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise DeadlineExceeded
        return left

    root = blobs_root / "ingest"
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f".tmp-{secrets.token_hex(8)}"
    tmp.mkdir(mode=0o700)
    try:
        remaining()
        try:
            read_tar(stream, tmp)  # the one blocking read; main's SIGALRM bounds it
            if after_read is not None:
                after_read()
            remaining()
            members = read_members(tmp)
            manifest = validate_members(members)
        except ArtifactError as error:
            raise IngestError(error.code) from None
        sha, version = manifest["material_sha256"], manifest["converter_version"]
        if upload_id is not None:
            material_id = await _claim_check(engine, upload_id, claim or "", sha, remaining())
        else:
            material = next((m for m in config.materials if m.sha256 == sha), None)
            if material is None:
                raise IngestError("unknown_material")
            material_id = material.id
        if version != ingest.CONVERTER_VERSION:
            raise IngestError("version_mismatch")
        scope = asyncio.timeout(remaining())
        try:
            async with scope, engine.connect():  # revision 3: no frozen version; the database is probed first
                pass
        except TimeoutError:
            if scope.expired():  # the deadline's own scope, not a driver's connect timeout
                raise DeadlineExceeded from None
            raise IngestError("database_unreachable") from None
        except Exception:
            raise IngestError("database_unreachable") from None
        artifact =artifact_id(members["manifest.json"])  # the bytes as received
        remaining()
        _store(tmp, root, artifact)
        state["stored"] = True
    except DeadlineExceeded:
        raise IngestError("timeout") from None
    finally:
        if not state["stored"]:
            shutil.rmtree(tmp, ignore_errors=True)
    try:
        args = {"material_sha256": sha, "converter_version": version, "artifact_id": artifact}
        async with asyncio.timeout(remaining()):
            result = await jobs.enqueue(KIND, {**args, "material_id": material_id})
    except (Exception, DeadlineExceeded):
        raise IngestError("enqueue_failed") from None
    if upload_id is not None:
        if before_mark is not None:
            before_mark()
        await _mark(engine, upload_id)
    return {"result": result.kind, "artifact_id": artifact}


async def _claim_check(engine: AsyncEngine, upload_id: str, claim: str, sha: str, left: float) -> str:
    """FR-INGEST-23 step 1, read only: the row exists with the artifact's sha256 and is `sending` under the
    claim with a live lease, or its import key already exists. Anything else is the cancel shape."""
    from reader.ingest import uploads

    try:
        async with asyncio.timeout(left), engine.connect() as conn:
            row = await uploads.row_by_id(conn, upload_id)
            ok = row is not None and row.sha256 == sha and (
                (row.state == "sending" and row.claim == claim and not row.expired)
                or await uploads.key_exists(conn, sha)
            )
    except (TimeoutError, OSError, ValueError):
        raise IngestError("database_unreachable") from None
    if not ok:
        raise Cancel
    return uploads.material_id(sha)


async def _mark(engine: AsyncEngine, upload_id: str) -> None:
    """Step 4: `received` (idempotent); a zero-row update is logged, and receive still answers its result."""
    async with engine.begin() as conn:
        done = await conn.execute(
            text("UPDATE uploads SET state = 'received', claim = NULL, lease_until = NULL, updated_at = now()"
                 " WHERE id = :i AND state <> 'received'"),
            {"i": upload_id},
        )
    if done.rowcount == 0:
        log.info("mark_noop id=%s", upload_id)


def _alarm(_: int, __: Any) -> None:
    raise DeadlineExceeded


def _claim_args(argv: list[str]) -> tuple[str | None, str | None]:
    parser = argparse.ArgumentParser(prog="reader.ingest.receive", add_help=False)
    parser.add_argument("--upload-id")
    parser.add_argument("--claim")
    args = parser.parse_args(argv)
    if (args.upload_id is None) != (args.claim is None):
        parser.error("--upload-id and --claim go together")
    valid = args.upload_id is None or (UUID_RE.fullmatch(args.upload_id) and CLAIM_RE.fullmatch(args.claim))
    if not valid:
        parser.error("invalid --upload-id or --claim")
    return args.upload_id, args.claim


def main(argv: list[str] | None = None) -> int:
    from reader.ingest.runtime import emit, run_command

    upload_id, claim = _claim_args([] if argv is None else argv)  # `python -m` passes sys.argv[1:] below
    signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, DEADLINE_S)
    with contextlib.suppress(Exception):
        os.umask(0o077)
    deadline = time.monotonic() + DEADLINE_S
    state: dict[str, bool] = {}

    def disarm() -> None:  # after the stdin read, asyncio.timeout bounds the awaits
        signal.setitimer(signal.ITIMER_REAL, 0)

    async def work(rt: Any) -> dict[str, Any]:
        return await receive(
            sys.stdin.buffer,
            config=rt.config,
            engine=rt.engine,
            jobs=rt.jobs,
            blobs_root=ingest.BLOBS_DIR,
            deadline=deadline,
            state=state,
            after_read=disarm,
            upload_id=upload_id,
            claim=claim,
        )

    try:
        return run_command(work)
    except DeadlineExceeded:
        emit({"error": "enqueue_failed" if state.get("stored") else "timeout"})
        return 1
    finally:
        disarm()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
