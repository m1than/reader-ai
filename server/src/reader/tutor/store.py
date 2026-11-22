"""`tutor_questions` rows, their guarded transitions, the stale-row rule, and the reads the handler needs
(SRV-TUTOR FR-TUTOR-1..5, -9, -10; INV-TUTOR-6, -7).

Every transition is `UPDATE … WHERE state = 'queued'`, so the api's finalisation and the worker's last write
cannot both win. Span texts are read from SRV-INGEST's `material_spans` directly (read-only) until its
`spans_by_ids` helper exists (cross-spec item).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from reader import blobs
from reader.tutor import params
from reader.tutor.answer import Cell
from reader.tutor.errors import TutorError

TERMINAL: Final = frozenset({"answered", "failed", "refused", "expired"})
_ONE_QUEUED: Final = "tutor_questions_one_queued"


@dataclass(frozen=True)
class Derived:
    state: str
    error_code: str | None
    busy: bool


def derive(row_state: str, job: str, age_s: float) -> Derived:
    """VEC-TUTOR-4: a queued row's state from its SRV-EVENTS job status and its age; terminal rows stay."""
    if row_state != "queued":
        return Derived(row_state, None, False)
    if job == "running":  # a running call finishes or its job dies
        return Derived("queued", None, True)
    if job in ("dead", "succeeded"):  # the handler always ends the row; a finished job left it queued
        return Derived("failed", "tutor_failed", False)
    if age_s > params.QUESTION_TTL_S:
        return Derived("expired", "expired", False)
    if job == "none" and age_s > params.STALE_WITHOUT_JOB_S:
        return Derived("failed", "lost", False)
    return Derived("queued", None, True)


async def insert_question(
    conn: AsyncConnection,
    owner_id: uuid.UUID,
    lesson_id: uuid.UUID,
    revision_sha256: str,
    cell_id: str | None,
    question: str,
) -> uuid.UUID:
    """A `queued` row; a second live question of the same user hits the partial unique index: `tutor_busy`."""
    try:
        async with conn.begin_nested():
            row = await conn.execute(
                text(
                    "INSERT INTO tutor_questions"
                    " (owner_id, lesson_id, revision_sha256, cell_id, question, state)"
                    " VALUES (:o, :l, :r, :c, :q, 'queued') RETURNING id"
                ),
                {"o": owner_id, "l": lesson_id, "r": revision_sha256, "c": cell_id, "q": question},
            )
            qid: uuid.UUID = row.scalar_one()
    except IntegrityError as error:
        if _ONE_QUEUED in str(error.orig):
            raise TutorError("tutor_busy") from None
        raise
    return qid


async def finish(
    conn: AsyncConnection,
    question_id: uuid.UUID,
    state: str,
    *,
    error_code: str | None = None,
    answer: Mapping[str, Any] | None = None,
    grounded: bool | None = None,
    model: str | None = None,
) -> bool:
    """The one transition out of `queued`; False when the row was no longer queued."""
    assert state in TERMINAL  # noqa: S101 (a programming error, never input)
    row = await conn.execute(
        text(
            "UPDATE tutor_questions SET state = :s, error_code = :e, answer = CAST(:a AS jsonb),"
            " grounded = :g, model = :m, finished_at = now() WHERE id = :i AND state = 'queued' RETURNING id"
        ),
        {
            "s": state,
            "e": error_code,
            "a": None if answer is None else json.dumps(answer, ensure_ascii=False),
            "g": grounded,
            "m": model,
            "i": question_id,
        },
    )
    return row.first() is not None


@dataclass(frozen=True)
class Question:
    id: uuid.UUID
    owner_id: uuid.UUID
    handle: str
    lesson_id: uuid.UUID
    revision_sha256: str
    cell_id: str | None
    question: str
    state: str
    age_s: float
    material_sha256: str | None
    course_title: str


async def load(conn: AsyncConnection, question_id: uuid.UUID) -> Question | None:
    row = (
        await conn.execute(
            text(
                "SELECT q.id, q.owner_id, u.handle, q.lesson_id, q.revision_sha256, q.cell_id, q.question,"
                " q.state, extract(epoch FROM now() - q.created_at), c.material_sha256, c.title"
                " FROM tutor_questions q JOIN users u ON u.id = q.owner_id"
                " JOIN lessons l ON l.id = q.lesson_id JOIN courses c ON c.id = l.course_id"
                " WHERE q.id = :i"
            ),
            {"i": question_id},
        )
    ).first()
    if row is None:
        return None
    return Question(
        row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], float(row[8]), row[9], row[10]
    )


async def queued_of(conn: AsyncConnection, owner_id: uuid.UUID) -> list[tuple[uuid.UUID, float]]:
    rows = await conn.execute(
        text(
            "SELECT id, extract(epoch FROM now() - created_at) FROM tutor_questions"
            " WHERE owner_id = :o AND state = 'queued'"
        ),
        {"o": owner_id},
    )
    return [(r[0], float(r[1])) for r in rows]


async def spans_by_ids(
    conn: AsyncConnection, owner_id: uuid.UUID, span_ids: Sequence[str]
) -> dict[str, tuple[int, str]]:
    """span id `<material_sha256>:<seq>` → (page, text), for the owner's materials only."""
    pairs = [s.rsplit(":", 1) for s in span_ids if ":" in s]
    keys = [(sha, int(seq)) for sha, seq in pairs if seq.isdigit()]
    if not keys:
        return {}
    rows = await conn.execute(
        text(
            "SELECT m.sha256, s.seq, s.page, s.text FROM material_spans s"
            " JOIN ingests i ON i.id = s.ingest_id JOIN materials m ON m.id = i.material_id"
            " JOIN unnest(CAST(:shas AS text[]), CAST(:seqs AS integer[])) AS w(sha, seq)"
            " ON w.sha = m.sha256 AND w.seq = s.seq"
            " WHERE s.owner_id = :o AND m.owner_id = :o"
        ),
        {"o": owner_id, "shas": [k[0] for k in keys], "seqs": [k[1] for k in keys]},
    )
    return {f"{r[0]}:{r[1]}": (int(r[2]), str(r[3])) for r in rows}


def _source(value: object) -> str:
    if isinstance(value, list):
        return "".join(str(v) for v in value)
    return value if isinstance(value, str) else ""


def revision_cells(root: Path, sha256: str) -> dict[str, Cell]:
    """The revision's cells by id: kind (`metadata.reader.kind`) and source. Blocking: run in a thread."""
    notebook = json.loads(blobs.get(root, sha256))
    cells: dict[str, Cell] = {}
    for raw in notebook.get("cells", []):
        cid = raw.get("id")
        if not isinstance(cid, str):
            continue
        reader = (raw.get("metadata") or {}).get("reader") or {}
        kind = reader.get("kind") if isinstance(reader, dict) else None
        kind = kind if isinstance(kind, str) else str(raw.get("cell_type", ""))
        cells[cid] = Cell(cid, kind, _source(raw.get("source")))
    return cells
