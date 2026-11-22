"""Tutor routes (SRV-TUTOR FR-TUTOR-1, -2, -10, -12; INV-TUTOR-1), declared as data for SRV-AUTH's ROUTERS.

A lesson the user cannot open, an unknown or malformed id and another user's or another lesson's question all
answer exactly like a missing one. Logs carry ids, lengths and codes, never a question.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, Final

import anyio.to_thread
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.responses import JSONResponse, Response

from reader import blobs
from reader.agent import ledger
from reader.api.http import Ctx, Route, error
from reader.lessons.access import can_open, lesson_uuid
from reader.lessons.store import current_sha256
from reader.tutor import params, store
from reader.tutor.answer import max_reserve_usd
from reader.tutor.errors import TutorError
from reader.tutor.jobs import job_key

log = logging.getLogger("reader.tutor")
# The api has no ReaderConfig: the daily pre-check prices the worst case with VEC-TUTOR-3's model. The
# worker's ledger check (SRV-AGENT FR-AGENT-12) is the authoritative one, with the configured model.
PRECHECK_MODEL: Final = "claude-sonnet-5-5"
_ALLOWED_C0: Final = frozenset("\n\t")


def _not_found() -> JSONResponse:
    return error(404, "not_found")


def valid_question(value: object) -> str | None:
    """FR-TUTOR-1: trimmed, 1..1000 characters, ≤ 3000 UTF-8 bytes, no C0 control but newline and tab."""
    if not isinstance(value, str):
        return None
    question = value.strip()
    if not 1 <= len(question) <= params.QUESTION_CHARS_MAX:
        return None
    if len(question.encode("utf-8", errors="replace")) > params.QUESTION_BYTES_MAX:
        return None
    if any(ord(ch) < 0x20 and ch not in _ALLOWED_C0 for ch in question):
        return None
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in question):  # lone surrogates cannot be stored as UTF-8
        return None
    return question


async def _openable(ctx: Ctx, conn: AsyncConnection) -> uuid.UUID | None:
    lesson = lesson_uuid(ctx.request.path_params["lesson_id"])
    if lesson is None or not await can_open(conn, ctx.signed_in.id, str(lesson)):
        return None
    return lesson


async def finalize_stale(ctx: Ctx, owner_id: uuid.UUID) -> None:
    """FR-TUTOR-10: a queued row whose job died, with no job after 60 s, or past its TTL becomes terminal."""
    jobs = getattr(ctx.request.app.state, "jobs", None)
    async with ctx.engine.connect() as conn:
        queued = await store.queued_of(conn, owner_id)
    for qid, age_s in queued:
        job = str(await jobs.job_status(params.KIND, job_key(qid))) if jobs is not None else "none"
        derived = store.derive("queued", job, age_s)
        if derived.state != "queued":
            async with ctx.engine.begin() as conn:
                if await store.finish(conn, qid, derived.state, error_code=derived.error_code):
                    log.info(
                        "tutor_finalised question=%s state=%s code=%s", qid, derived.state, derived.error_code
                    )


def _cells(root: Path, sha256: str) -> set[str]:
    return set(store.revision_cells(root, sha256))


async def _revision_ok(conn: AsyncConnection, lesson: uuid.UUID, revision: object) -> bool:
    if not blobs.is_sha256(revision):
        return False
    found = await conn.execute(
        text("SELECT 1 FROM lesson_revisions WHERE lesson_id = :l AND sha256 = :s"),
        {"l": lesson, "s": revision},
    )
    return found.scalar() is not None


async def ask(ctx: Ctx) -> Response:
    user = ctx.signed_in.id
    body = await ctx.json()  # read before any database connection is taken (a slow client holds none)
    async with ctx.engine.connect() as conn:
        lesson = await _openable(ctx, conn)
        if lesson is None:
            return _not_found()
        question = valid_question(body.get("question") if isinstance(body, dict) else None)
        if question is None:
            log.info("tutor_ask lesson=%s outcome=question_invalid", lesson)
            return error(400, "question_invalid")
        revision = body.get("revision")
        if revision is None:
            revision = await current_sha256(conn, lesson)
        if not await _revision_ok(conn, lesson, revision):
            return error(400, "unknown_revision")
    cell_id = body.get("cell_id")
    if cell_id is not None:
        root: Path = getattr(ctx.request.app.state, "blobs_root", blobs.DEFAULT_ROOT)
        try:
            cells = await anyio.to_thread.run_sync(_cells, root, revision)
        except (blobs.BlobMissing, blobs.BlobCorrupt, ValueError):
            log.warning("tutor_ask lesson=%s outcome=blob_unavailable", lesson)
            return error(500, "tutor_failed")
        if not isinstance(cell_id, str) or cell_id not in cells:
            return error(400, "cell_unknown")
    await finalize_stale(ctx, user)
    async with ctx.engine.connect() as conn:
        if await store.queued_of(conn, user):
            log.info("tutor_ask lesson=%s outcome=tutor_busy", lesson)
            return error(409, "tutor_busy")
    today = await ledger.spent(ctx.engine, owner_id=user, task=params.TASK, day=True)  # FR-AGENT-12's scope
    if today + max_reserve_usd(PRECHECK_MODEL) > params.DAILY_CAP_USD:
        log.info("tutor_ask lesson=%s outcome=daily_budget_exhausted", lesson)
        return error(429, "daily_budget_exhausted")
    try:
        async with ctx.engine.begin() as conn:
            qid = await store.insert_question(conn, user, lesson, str(revision), cell_id, question)
    except TutorError as refused:
        return error(409, refused.code)
    outcome = await _enqueue(ctx, qid)
    log.info("tutor_ask lesson=%s question=%s chars=%d outcome=%s", lesson, qid, len(question), outcome)
    return JSONResponse({"question_id": str(qid)}, status_code=202)


async def _enqueue(ctx: Ctx, qid: uuid.UUID) -> str:
    jobs = getattr(ctx.request.app.state, "jobs", None)
    try:
        if jobs is None:
            raise LookupError("no Jobs on the app")
        await jobs.enqueue(params.KIND, {"question_id": str(qid)})
    except Exception as failure:  # the row must not stay queued (INV-TUTOR-6)
        log.warning("tutor_enqueue_failed question=%s error=%s", qid, type(failure).__name__)
        async with ctx.engine.begin() as conn:
            await store.finish(conn, qid, "failed", error_code="tutor_failed")
        return "tutor_failed"
    return "queued"


def _state(row: Any) -> dict[str, Any]:
    state, answer, grounded, code = row
    out: dict[str, Any] = {"state": state}
    if state == "answered":
        out["answer"], out["grounded"] = answer, grounded
    if code is not None:
        out["error_code"] = code
    return out


async def get_question(ctx: Ctx) -> Response:
    qid = lesson_uuid(ctx.request.path_params["question_id"])
    async with ctx.engine.connect() as conn:
        lesson = await _openable(ctx, conn)
    if lesson is None or qid is None:
        return _not_found()
    await finalize_stale(ctx, ctx.signed_in.id)
    async with ctx.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT state, answer, grounded, error_code FROM tutor_questions"
                    " WHERE id = :q AND owner_id = :u AND lesson_id = :l"
                ),
                {"q": qid, "u": ctx.signed_in.id, "l": lesson},
            )
        ).first()
    if row is None:
        return _not_found()
    return JSONResponse(_state(tuple(row)))


async def list_questions(ctx: Ctx) -> Response:
    async with ctx.engine.connect() as conn:
        lesson = await _openable(ctx, conn)
    if lesson is None:
        return _not_found()
    await finalize_stale(ctx, ctx.signed_in.id)
    async with ctx.engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT id, question, cell_id, state, grounded, error_code, created_at FROM tutor_questions"
                " WHERE owner_id = :u AND lesson_id = :l ORDER BY created_at DESC, id LIMIT :n"
            ),
            {"u": ctx.signed_in.id, "l": lesson, "n": params.HISTORY_MAX},
        )
        items = [
            {
                "question_id": str(r[0]),
                "question": r[1],
                "cell_id": r[2],
                "state": r[3],
                "grounded": r[4],
                "error_code": r[5],
                "created_at": r[6].isoformat(),
            }
            for r in rows
        ]
    return JSONResponse({"questions": items})


ROUTES: Final[tuple[Route, ...]] = (
    Route("POST", "/api/lessons/{lesson_id}/ask", "session", ask),
    Route("GET", "/api/lessons/{lesson_id}/questions/{question_id}", "session", get_question),
    Route("GET", "/api/lessons/{lesson_id}/questions", "session", list_questions),
)
