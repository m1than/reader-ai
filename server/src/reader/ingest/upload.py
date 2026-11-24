"""`POST /api/materials/uploads?language=L`: store a raw PDF body in the inbox without parsing it.

FR-INGEST-18's order: Content-Type, the instance's upload lock, the inbox directory, the crash sweep,
reader.yaml, the streamed body (cap and deadline), the magic check (head and tail kept in memory while
streaming), dedupe, quota, id collision, then fsync, rename to `<sha256>.pdf` (0440), the row and NOTIFY.
Every failure path removes the temporary file and releases the lock. The filename is display metadata only
and is never logged (FR-INGEST-24).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import secrets
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from sqlalchemy import text
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, Response

import reader.ingest as ingest
from reader.api.http import Ctx, error
from reader.config.model import Material
from reader.ingest import magic, params, uploads
from reader.ingest.filename import FilenameError, clean
from reader.ingest.fsio import fsync_dir

log = logging.getLogger("reader.ingest")

INBOX_NAME: Final = re.compile(r"[0-9a-f]{64}\.pdf")
TMP_PREFIX: Final = ".tmp-"


class Refused(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status, self.code = status, code


def blobs_root(ctx: Ctx) -> Path:
    return Path(getattr(ctx.request.app.state, "blobs_root", ingest.BLOBS_DIR))


def inbox_dir(ctx: Ctx) -> Path:
    return blobs_root(ctx) / params.INBOX


def _media_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def _ensure_inbox(root: Path) -> None:
    """Created 0750 by the api on first use; an existing entry must be a real directory of the service uid."""
    try:
        os.mkdir(root, 0o750)
        os.chmod(root, 0o750)  # noqa: S103 (the spec's mode: the service uid and its group only)
    except FileExistsError:
        pass
    info = os.lstat(root)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        log.warning("inbox_invalid")
        raise Refused(500, "inbox_invalid")


async def _sweep(ctx: Ctx, root: Path) -> None:
    """Crash leftovers: every `.tmp-*` (its writer would hold the lock), every `<sha>.pdf` without a row."""
    async with ctx.engine.connect() as conn:
        known = {r[0] for r in await conn.execute(text("SELECT sha256 FROM uploads"))}
    removed = other = 0
    for entry in os.scandir(root):
        name = entry.name
        leftover = name.startswith(TMP_PREFIX) or (INBOX_NAME.fullmatch(name) and name[:64] not in known)
        if leftover:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(entry.path)
            removed += 1
        elif not INBOX_NAME.fullmatch(name):
            other += 1
    log.info("inbox_sweep removed=%d other=%d", removed, other)


@dataclass
class Body:
    path: Path
    size: int = 0
    head: bytes = b""
    tail: bytes = b""
    sha256: str = ""


def _write_body(path: Path) -> int:
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


async def _stream(ctx: Ctx, body: Body) -> None:
    fd = _write_body(body.path)
    digest = hashlib.sha256()
    try:
        async with asyncio.timeout(params.BODY_DEADLINE_S):
            async for chunk in ctx.request.stream():
                for start in range(0, len(chunk), params.CHUNK):
                    piece = chunk[start : start + params.CHUNK]
                    if body.size + len(piece) > params.UPLOAD_BYTES_MAX:
                        raise Refused(413, "too_large")
                    await asyncio.to_thread(_write_all, fd, piece)  # off the event loop (code review round 1)
                    digest.update(piece)
                    if len(body.head) < 8:
                        body.head = (body.head + piece)[:8]
                    body.tail = (body.tail + piece)[-magic.TAIL_BYTES :]
                    body.size += len(piece)
        await asyncio.to_thread(os.fsync, fd)
    except TimeoutError:
        raise Refused(408, "body_timeout") from None
    finally:
        os.close(fd)
    body.sha256 = digest.hexdigest()


async def _store(ctx: Ctx, root: Path, body: Body, filename: str, language: str) -> dict[str, Any]:
    final = root / f"{body.sha256}.pdf"
    os.rename(body.path, final)
    os.chmod(final, 0o440)
    fsync_dir(root)
    async with ctx.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO uploads (owner_id, sha256, size, filename, language, state)"
                " VALUES (:o, :s, :n, :f, :l, 'queued')"
            ),
            {"o": ctx.signed_in.id, "s": body.sha256, "n": body.size, "f": filename, "l": language},
        )
        await conn.execute(text(f"SELECT pg_notify('{params.NOTIFY_CHANNEL}', '')"))
        row = await uploads.row_by_sha(conn, body.sha256)
        assert row is not None  # noqa: S101 (inserted in this transaction)
        view = await uploads.view_of(conn, row)
    log.info(
        "upload_stored id=%s sha=%s size=%d language=%s", view["id"], body.sha256[:12], body.size, language
    )
    return view


async def _admit(ctx: Ctx, body: Body, materials: Sequence[Material]) -> Response | None:
    """Steps 8 to 10: dedupe (before the quota), quota, id collision. A response ends the upload. Bytes
    whose material row is deleted are refused first (FR-INGEST-31: restore it instead), before any row is
    written."""
    async with ctx.engine.connect() as conn:
        deleted = await conn.execute(
            text("SELECT 1 FROM materials WHERE owner_id = :o AND sha256 = :s AND deleted_at IS NOT NULL"),
            {"o": ctx.signed_in.id, "s": body.sha256},
        )
        if deleted.first() is not None:
            return error(409, "material_deleted")
    declared = next((m for m in materials if m.sha256 == body.sha256), None)
    if declared is not None:
        return JSONResponse({"material_id": declared.id, "upload": None})
    await uploads.expire(ctx.engine)
    async with ctx.engine.connect() as conn:
        existing = await uploads.row_by_sha(conn, body.sha256)
        if existing is not None:
            return JSONResponse(await uploads.view_of(conn, existing))
        count, total, _ = await uploads.open_usage(conn)
        if count + 1 > params.OPEN_MAX or total + body.size > params.BYTES_MAX:
            log.info("upload_refused code=inbox_full open=%d bytes=%d size=%d", count, total, body.size)
            return error(409, "inbox_full")
        wanted = uploads.material_id(body.sha256)
        clash = (
            await conn.execute(
                text("SELECT 1 FROM uploads WHERE sha256 LIKE :p AND sha256 <> :s"),
                {"p": body.sha256[:12] + "%", "s": body.sha256},
            )
        ).first()
    if clash is not None or any(m.id == wanted for m in materials):
        return error(409, "id_collision")
    return None


async def handle(ctx: Ctx, materials_of: Callable[[], Sequence[Material]]) -> Response:
    if ctx.signed_in.role != "owner":
        return error(403, "owner_only")
    language = ctx.request.query_params.get("language", "")
    if language not in params.LANGUAGES:
        return error(400, "language")
    try:
        filename = clean(ctx.request.headers.get("x-reader-filename", ""))
    except FilenameError:
        return error(400, "filename")
    if _media_type(ctx.request.headers.get("content-type", "")) != "application/pdf":
        return error(415, "content_type")
    declared = ctx.request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > params.UPLOAD_BYTES_MAX:
        return error(413, "too_large")
    body: Body | None = None
    try:
        async with uploads.upload_lock(ctx.engine):
            root = inbox_dir(ctx)
            _ensure_inbox(root)
            await _sweep(ctx, root)
            materials = materials_of()  # 503 config_unavailable (the caller's guard) before any byte is read
            body = Body(root / f"{TMP_PREFIX}{secrets.token_hex(16)}")
            await _stream(ctx, body)
            if body.size == 0:
                raise Refused(400, "empty")
            if not magic.check(body.head, body.tail):
                raise Refused(415, "not_pdf")
            refused = await _admit(ctx, body, materials)
            if refused is not None:
                return refused
            view = await _store(ctx, root, body, filename, language)
            body = None  # renamed: nothing to remove
            return JSONResponse(view, status_code=201)
    except uploads.Busy:
        return error(409, "upload_busy")
    except Refused as refusal:
        log.info("upload_refused code=%s", refusal.code)
        return error(refusal.status, refusal.code)
    except ClientDisconnect:
        log.info("upload_refused code=disconnected")
        return error(400, "disconnected")
    finally:
        if body is not None:
            with contextlib.suppress(FileNotFoundError):
                body.path.unlink()
