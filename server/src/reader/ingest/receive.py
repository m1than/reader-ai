"""`python -I -m reader.ingest.receive`: validate a tar from stdin, store it, enqueue it (FR-INGEST-11).

Phases: read and validate, store (the final rename), enqueue. A rejection, or the 300 s deadline before
storage completes, leaves nothing under `<blobs>/ingest/` but this run's own temporary directory, which it
removes. After storage the content-addressed directory is valid and stays, whatever happens next
(`enqueue_failed`).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import shutil
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO, Final, Protocol

from sqlalchemy.ext.asyncio import AsyncEngine

import reader.ingest as ingest
from reader.config import ReaderConfig
from reader.ingest import store
from reader.ingest.artifact import ArtifactError, artifact_id, read_members, validate_dir, validate_members
from reader.ingest.errors import IngestError
from reader.ingest.jobs import KIND
from reader.ingest.tarstream import read_tar

DEADLINE_S: Final = 300.0


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
) -> dict[str, Any]:
    """`state["stored"]` turns True once the final rename is done, for callers that see the deadline fire."""
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
        material = next((m for m in config.materials if m.sha256 == sha), None)
        if material is None:
            raise IngestError("unknown_material")
        if version != ingest.CONVERTER_VERSION:
            raise IngestError("version_mismatch")
        try:
            async with asyncio.timeout(remaining()), engine.connect() as conn:
                frozen = await store.frozen_version(conn, sha)
        except TimeoutError:
            raise DeadlineExceeded from None
        except Exception:
            raise IngestError("database_unreachable") from None
        if frozen is not None and frozen != version:
            raise IngestError("version_frozen")
        artifact = artifact_id(members["manifest.json"])  # the bytes as received
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
            result = await jobs.enqueue(KIND, {**args, "material_id": material.id})
    except (Exception, DeadlineExceeded):
        raise IngestError("enqueue_failed") from None
    return {"result": result.kind, "artifact_id": artifact}


def _alarm(_: int, __: Any) -> None:
    raise DeadlineExceeded


def main() -> int:
    from reader.ingest.runtime import emit, run_command

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
        )

    try:
        return run_command(work)
    except DeadlineExceeded:
        emit({"error": "enqueue_failed" if state.get("stored") else "timeout"})
        return 1
    finally:
        disarm()


if __name__ == "__main__":
    sys.exit(main())
