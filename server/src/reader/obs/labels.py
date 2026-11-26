"""SRV-OBS FR-OBS-24 (ADR-0038): owner labels. Postgres `owner_labels` is the record; MLflow gets a HUMAN
assessment mirror on the labelled thing's trace, found again by assessment_name + metadata.revision, so a
retried job never posts twice. A relabel posts a new assessment that `overrides` the previous one (round 8). A
job whose revision is below the row's makes no request: a newer label's job will mirror it. A trace that is
missing follows FR-OBS-14's 404 rule: transient while attempts remain, then a no-trace log, never dead."""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text

from reader.jobs.model import JobKind
from reader.obs import phase2
from reader.obs.judge_labels import NotLabelable, parse_item
from reader.obs.phase2 import MlflowRefused, request

log = logging.getLogger("reader.obs")
KIND: Final = "obs.label"
REASONS: Final = tuple(phase2.VEC["labels"]["reasons"])
TARGETS: Final = phase2.VEC["labels"]["targets"]
SOURCE: Final = dict(phase2.VEC["labels"]["source"])
UUID_RE: Final = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
ITEM_RE: Final = r"^(ji|es):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
MAX_ATTEMPTS: Final = 3
# ADR-0039 (FR-OBS-24 as amended): judge labels per slot; judge.link_type joins with SRV-KB, judge.align never.
JUDGE_SLOTS: Final = tuple(phase2.VEC["labels"]["judge_slots"])
NOTE_MAX: Final = int(phase2.VEC["labels"]["note_max_chars"])
__all__ = ["NotLabelable"]


def assessment_name(slot: str) -> str:
    """`owner.<slot>_ok` with the slot's dots as underscores (B7 labels item 1)."""
    return f"owner.{slot.replace('.', '_')}_ok"


def check_slots(slots: Iterable[str]) -> None:
    """Refuses a slot list whose names collide after the dot-to-underscore mapping (checked at load)."""
    names = [assessment_name(s) for s in slots]
    if len(set(names)) != len(names):
        raise ValueError("judge slot names are not injective")


check_slots(JUDGE_SLOTS)


class ObsLabelRefused(Exception):
    """MLflow answered 4xx other than 404: the key ends dead with this class name."""


class TraceMissing(Exception):
    """MLflow answered 404 for the labelled thing's trace: transient while attempts remain."""


@dataclass(frozen=True)
class Label:
    target: str
    target_id: str
    ok: bool
    reason: str | None
    revision: int
    assessment_id: str | None
    slot: str | None = None


class LabelBody(BaseModel):
    """The route's body: exactly these keys. Lesson and answer labels carry no free text; a judge label may carry
    a one-line `note` (Postgres only, never mirrored, traced or logged; ADR-0039 B7 labels item 1)."""

    model_config = ConfigDict(extra="forbid", strict=True)
    target: Literal["lesson", "answer", "judge"]
    id: str
    ok: bool
    reason: Literal["wrong_fact", "unclear", "off_topic", "broken_lab", "bad_citation", "other"] | None = None
    note: str | None = Field(default=None, max_length=NOTE_MAX, pattern=r"^[^\n\r]*$")

    @model_validator(mode="after")
    def _shape(self) -> LabelBody:
        judge = self.target == "judge"
        if not re.fullmatch(ITEM_RE if judge else UUID_RE, self.id):
            raise ValueError("id")
        if (judge and self.reason is not None) or (not judge and self.note is not None):
            raise ValueError("reason or note")
        return self


def _metadata(label: Label) -> dict[str, str]:
    if label.target == "judge":
        key = "judge_input_id" if label.slot == "judge.find" else "score_id"
        return {key: label.target_id, "revision": str(label.revision)}
    meta = {("lesson_id" if label.target == "lesson" else "question_id"): label.target_id}
    if label.reason is not None:
        meta["reason"] = label.reason
    meta["revision"] = str(label.revision)
    return meta


def _missing(last: bool, target: str) -> None:
    """FR-OBS-24 (c) with FR-OBS-14's 404 rule (code review round 1 of phase 2, B4)."""
    if not last:
        raise TraceMissing(target)
    log.info("obs label no_trace target=%s", target)


async def mirror(
    label: Label, *, job_revision: int, trace_ref: str, client: httpx.AsyncClient, last: bool = False
) -> str | None:
    """The mirror's assessment id, or None when this job's revision is below the row's (no request) or, on the
    last attempt, the trace is missing."""
    if job_revision < label.revision:
        return None
    name = assessment_name(str(label.slot)) if label.target == "judge" else TARGETS[label.target]["assessment"]
    status, got = await request(
        client, "GET", "/ajax-api/3.0/mlflow/traces/get", params={"trace_id": trace_ref}, ok404=True
    )
    if status == 404:
        _missing(last, label.target)
        return None
    info = (got.get("trace") or {}).get("trace_info") or {}
    for there in info.get("assessments") or []:
        meta = there.get("metadata") or {}
        if there.get("assessment_name") == name and str(meta.get("revision")) == str(label.revision):
            return str(there["assessment_id"])
    assessment: dict[str, Any] = {
        "assessment_name": name,
        "trace_id": trace_ref,
        "source": dict(SOURCE),
        "feedback": {"value": label.ok},
        "metadata": _metadata(label),
    }
    if label.assessment_id:
        assessment["overrides"] = label.assessment_id
    path = f"/api/3.0/mlflow/traces/{trace_ref}/assessments"
    status, posted = await request(client, "POST", path, {"assessment": assessment}, ok404=True)
    if status == 404:  # the trace went away between the GET and the POST
        _missing(last, label.target)
        return None
    return str((posted.get("assessment") or {})["assessment_id"])


# --- the record (the route) ---

# FR-OBS-24 (a): a lesson label is for the lesson's current revision (FR-LESSON-4's current_sha256: the newest
# stage record), the one the owner sees; an answer label for the question's revision.
LESSON_SQL: Final = (
    "SELECT coalesce((SELECT s.sha256 FROM lesson_revision_stages s WHERE s.lesson_id = l.id"
    " ORDER BY s.id DESC LIMIT 1), '') FROM lessons l WHERE l.id = :id AND l.owner_id = :o"
)
ANSWER_SQL: Final = "SELECT revision_sha256 FROM tutor_questions WHERE id = :id AND owner_id = :o"
UPSERT_SQL: Final = (
    "INSERT INTO owner_labels (id, owner_id, target, target_id, lesson_revision_sha256, ok, reason, revision)"
    " VALUES (:id, :o, :t, :tid, :rev, :ok, :reason, 1)"
    " ON CONFLICT (owner_id, target, target_id, lesson_revision_sha256) DO UPDATE SET ok = EXCLUDED.ok,"
    " reason = EXCLUDED.reason, revision = owner_labels.revision + 1, updated_at = now()"
    " RETURNING id, revision"
)


JUDGE_INPUT_SQL: Final = "SELECT input_sha256, labelable FROM judge_inputs WHERE id = :id"
JUDGE_SCORE_SQL: Final = (
    "SELECT s.slot, s.eval_id, s.record_id, s.judge, ji.input_sha256, ji.labelable,"
    " (s.judge_input_id IS NOT NULL) AS sampled FROM obs_eval_scores s"
    " LEFT JOIN judge_inputs ji ON ji.id = s.judge_input_id WHERE s.id = :id AND s.owner_id = :o"
)
JUDGE_UPSERT_SQL: Final = (
    "INSERT INTO owner_labels (id, owner_id, target, target_id, lesson_revision_sha256, ok, reason, revision, slot,"
    " note) VALUES (:id, :o, 'judge', :tid, :rev, :ok, NULL, 1, :slot, :note)"
    " ON CONFLICT (owner_id, target, target_id, lesson_revision_sha256) DO UPDATE SET ok = EXCLUDED.ok,"
    " note = EXCLUDED.note, revision = owner_labels.revision + 1, updated_at = now()"
    " RETURNING id, revision"
)
SLOT_COUNT_SQL: Final = "SELECT count(*) FROM owner_labels WHERE owner_id = :o AND target = 'judge' AND slot = :s"


async def _judge_target(conn: Any, owner: uuid.UUID, item: str) -> tuple[str, str, str] | None:
    """(target id, slot, the judged input's sha256), None when missing; NotLabelable without consent."""
    parsed = parse_item(item)
    if parsed is None:
        return None
    kind, ident = parsed
    if kind == "ji":
        row = (await conn.execute(text(JUDGE_INPUT_SQL), {"id": ident})).first()
        if row is None:
            return None
        if not row.labelable:
            raise NotLabelable(item)
        return str(ident), "judge.find", str(row.input_sha256)
    row = (await conn.execute(text(JUDGE_SCORE_SQL), {"id": ident, "o": owner})).first()
    if row is None:
        return None
    if row.sampled:
        if not row.labelable:
            raise NotLabelable(item)
        return str(ident), str(row.slot), str(row.input_sha256)
    seen = hashlib.sha256(f"es:{row.eval_id}:{row.record_id}:{row.judge}".encode()).hexdigest()
    return str(ident), str(row.slot), seen


async def _record_judge(conn: Any, owner: uuid.UUID, body: LabelBody, mirror_on: bool) -> tuple[int, str] | None:
    target = await _judge_target(conn, owner, body.id)
    if target is None:
        return None
    target_id, slot, seen = target
    row = (
        await conn.execute(
            text(JUDGE_UPSERT_SQL),
            {"id": uuid.uuid4(), "o": owner, "tid": target_id, "rev": seen, "ok": body.ok, "slot": slot,
             "note": body.note},
        )
    ).one()
    label_id, revision = str(row.id), int(row.revision)
    if mirror_on:
        await phase2.stage(conn, KIND_DECL, {"label_id": label_id, "revision": revision})
    if revision == 1:  # a new label (a relabel does not count): FR-OBS-41's trigger
        count = int((await conn.execute(text(SLOT_COUNT_SQL), {"o": owner, "s": slot})).scalar_one())
        await _maybe_align(conn, owner, slot, count)
    return revision, label_id


async def _maybe_align(conn: Any, owner: uuid.UUID, slot: str, count: int) -> None:
    """FR-OBS-41: every 50th judge label of a slot starts an alignment proposal in this transaction."""
    import importlib

    try:
        align = importlib.import_module("reader.obs.align")
    except ModuleNotFoundError:
        return
    await align.start_if_due(conn, owner, slot, count)


async def record(conn: Any, owner: uuid.UUID, body: LabelBody, *, mirror_on: bool) -> tuple[int, str] | None:
    """(revision, label id) after the upsert, or None when the target is not the owner's. With tracing on, the
    outbox `obs.label` key row is written in the same transaction. A judge target without consent raises
    NotLabelable (409)."""
    if body.target == "judge":
        return await _record_judge(conn, owner, body, mirror_on)
    found = (
        await conn.execute(
            text(LESSON_SQL if body.target == "lesson" else ANSWER_SQL), {"id": body.id, "o": owner}
        )
    ).first()
    if found is None:
        return None
    row = (
        await conn.execute(
            text(UPSERT_SQL),
            {
                "id": uuid.uuid4(),
                "o": owner,
                "t": body.target,
                "tid": body.id,
                "rev": found[0] or "",
                "ok": body.ok,
                "reason": body.reason,
            },
        )
    ).one()
    label_id, revision = str(row.id), int(row.revision)
    if mirror_on:
        await phase2.stage(conn, KIND_DECL, {"label_id": label_id, "revision": revision})
    return revision, label_id


# --- the job ---


class LabelArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label_id: str = Field(pattern=UUID_RE)
    revision: int = Field(ge=1, le=10000)


ROW_SQL: Final = (
    "SELECT owner_id, target, target_id, lesson_revision_sha256, ok, reason, revision, assessment_id, slot"
    " FROM owner_labels WHERE id = :id"
)
JUDGE_TRACE_SQL: Final = (
    "SELECT coalesce((SELECT judge_trace_id FROM judge_inputs WHERE id = :tid),"
    " (SELECT trace_id FROM obs_eval_scores WHERE id = :tid))"
)


async def judge_trace_ref(conn: Any, row: Any) -> str | None:
    """A judge label's mirror trace: the judge.find job's trace, or the score's own trace (FR-OBS-24 c)."""
    found = (await conn.execute(text(JUDGE_TRACE_SQL), {"tid": row.target_id})).scalar()
    return f"tr-{str(found).strip()}" if found else None
# FR-OBS-24 (c), one row per label: the run that produced the labelled revision (newest by created_at, id), in
# it the revise stage's job, else the draft stage's (a converged first pass has no revise), newest key by
# (created_at, job_id). A revision no compile run produced has no trace.
LESSON_JOB_SQL: Final = (
    "SELECT k.job_id, k.key FROM job_keys k WHERE k.kind = 'pipeline.stage' AND k.job_id IS NOT NULL"
    " AND k.args->>'stage' IN ('revise', 'draft')"
    " AND k.args->>'run_id' = (SELECT r.id::text FROM compile_runs r"
    " WHERE r.lesson_id = :tid AND r.owner_id = :o AND r.final_sha256 = :rev AND :rev <> ''"
    " ORDER BY r.created_at DESC, r.id DESC LIMIT 1)"
    " ORDER BY (k.args->>'stage' = 'revise') DESC, k.created_at DESC, k.job_id DESC LIMIT 1"
)
# Fallback (code review round 2): a run that stopped before finishing has no final_sha256, yet its draft or
# revise step may have produced the labelled revision; that step's own job, newest run first.
LESSON_STEP_JOB_SQL: Final = (
    "SELECT k.job_id, k.key FROM compile_steps s JOIN compile_runs r ON r.id = s.run_id AND r.owner_id = :o"
    " JOIN job_keys k ON k.kind = 'pipeline.stage' AND k.job_id IS NOT NULL"
    " AND k.args->>'run_id' = r.id::text AND k.args->>'step' = s.step::text"
    " WHERE r.lesson_id = :tid AND s.owner_id = :o AND s.stage IN ('revise', 'draft') AND :rev <> ''"
    " AND s.output_sha256 = :rev"
    " ORDER BY r.created_at DESC, r.id DESC, s.step DESC, k.created_at DESC, k.job_id DESC LIMIT 1"
)
ANSWER_JOB_SQL: Final = (  # the question must be the label owner's (owner-scoped, code review round 2)
    "SELECT k.job_id, k.key FROM job_keys k WHERE k.kind = 'tutor.answer' AND k.job_id IS NOT NULL"
    " AND k.args->>'question_id' = CAST(:tid AS text)"
    " AND EXISTS (SELECT 1 FROM tutor_questions q WHERE q.id = CAST(:tid AS uuid) AND q.owner_id = :o)"
    " ORDER BY k.created_at DESC, k.job_id DESC LIMIT 1"
)
SAVE_SQL: Final = "UPDATE owner_labels SET assessment_id = :a WHERE id = :id AND revision = :r"


def trace_ref(job_id: int, key: str) -> str:
    from reader.obs.ids import derive_trace_id

    return f"tr-{derive_trace_id(job_id, key):032x}"


async def labelled_job(conn: Any, row: Any) -> Any:
    """The (job_id, key) of the labelled thing's traced job, or None (FR-OBS-24 c)."""
    params = {"tid": str(row.target_id), "o": row.owner_id, "rev": row.lesson_revision_sha256 or ""}
    sql = LESSON_JOB_SQL if row.target == "lesson" else ANSWER_JOB_SQL
    found = (await conn.execute(text(sql), params)).first()
    if found is None and row.target == "lesson":  # a run that stopped before finishing
        found = (await conn.execute(text(LESSON_STEP_JOB_SQL), params)).first()
    return found


async def handle(args: LabelArgs, conn: Any, ctx: Any) -> None:
    from reader.obs import setup as obs_setup

    if not obs_setup.current_settings().enabled:
        return
    row = (await conn.execute(text(ROW_SQL), {"id": args.label_id})).first()
    if row is None:
        return
    if row.target == "judge":
        ref = await judge_trace_ref(conn, row)
    else:
        job = await labelled_job(conn, row)
        ref = None if job is None else trace_ref(int(job.job_id), job.key)
    if ref is None:
        log.info("obs label no_trace target=%s", row.target)
        return
    label = Label(
        target=row.target,
        target_id=str(row.target_id),
        ok=bool(row.ok),
        reason=row.reason,
        revision=int(row.revision),
        assessment_id=row.assessment_id,
        slot=row.slot,
    )
    last = int(getattr(ctx, "attempts", 0)) + 1 >= MAX_ATTEMPTS
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            got = await mirror(label, job_revision=args.revision, trace_ref=ref, client=client, last=last)
    except MlflowRefused as refused:
        raise ObsLabelRefused(str(refused.status)) from None
    if got is not None:
        await conn.execute(text(SAVE_SQL), {"a": got, "id": args.label_id, "r": args.revision})


KIND_DECL: Final = JobKind(
    name=KIND,
    queue="obs",
    args=LabelArgs,
    key=("label_id", "revision"),
    handler=handle,
    max_attempts=MAX_ATTEMPTS,
    transient=(TraceMissing, httpx.TransportError, httpx.HTTPStatusError),
    timeout_s=30,
)
