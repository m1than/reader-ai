"""SRV-OBS FR-OBS-24 (ADR-0038): owner labels. Postgres `owner_labels` is the record; MLflow gets a HUMAN
assessment mirror on the labelled thing's trace, found again by assessment_name + metadata.revision, so a
retried job never posts twice. A relabel posts a new assessment that `overrides` the previous one (round 8). A
job whose revision is below the row's makes no request: a newer label's job will mirror it."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Final, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from reader.jobs.model import JobKind
from reader.obs import phase2
from reader.obs.phase2 import MlflowRefused, request

log = logging.getLogger("reader.obs")
KIND: Final = "obs.label"
REASONS: Final = tuple(phase2.VEC["labels"]["reasons"])
TARGETS: Final = phase2.VEC["labels"]["targets"]
SOURCE: Final = dict(phase2.VEC["labels"]["source"])
UUID_RE: Final = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


class ObsLabelRefused(Exception):
    """MLflow answered 4xx: the key ends dead with this class name."""


@dataclass(frozen=True)
class Label:
    target: str
    target_id: str
    ok: bool
    reason: str | None
    revision: int
    assessment_id: str | None


class LabelBody(BaseModel):
    """The route's body: exactly these keys (FR-OBS-24: no free text)."""

    model_config = ConfigDict(extra="forbid", strict=True)
    target: Literal["lesson", "answer"]
    id: str = Field(pattern=UUID_RE)
    ok: bool
    reason: Literal["wrong_fact", "unclear", "off_topic", "broken_lab", "bad_citation", "other"] | None = None


def _metadata(label: Label) -> dict[str, str]:
    meta = {("lesson_id" if label.target == "lesson" else "question_id"): label.target_id}
    if label.reason is not None:
        meta["reason"] = label.reason
    meta["revision"] = str(label.revision)
    return meta


async def mirror(label: Label, *, job_revision: int, trace_ref: str, client: httpx.AsyncClient) -> str | None:
    """The mirror's assessment id, or None when this job's revision is below the row's (no request)."""
    if job_revision < label.revision:
        return None
    name = TARGETS[label.target]["assessment"]
    _, got = await request(client, "GET", "/ajax-api/3.0/mlflow/traces/get", params={"trace_id": trace_ref})
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
    _, posted = await request(
        client, "POST", f"/api/3.0/mlflow/traces/{trace_ref}/assessments", {"assessment": assessment}
    )
    return str((posted.get("assessment") or {})["assessment_id"])


# --- the record (the route) ---

LESSON_SQL: Final = "SELECT published_sha256 FROM lessons WHERE id = :id AND owner_id = :o"
ANSWER_SQL: Final = "SELECT revision_sha256 FROM tutor_questions WHERE id = :id AND owner_id = :o"
UPSERT_SQL: Final = (
    "INSERT INTO owner_labels (id, owner_id, target, target_id, lesson_revision_sha256, ok, reason, revision)"
    " VALUES (:id, :o, :t, :tid, :rev, :ok, :reason, 1)"
    " ON CONFLICT (owner_id, target, target_id, lesson_revision_sha256) DO UPDATE SET ok = EXCLUDED.ok,"
    " reason = EXCLUDED.reason, revision = owner_labels.revision + 1, updated_at = now()"
    " RETURNING id, revision"
)


async def record(conn: Any, owner: uuid.UUID, body: LabelBody, *, mirror_on: bool) -> tuple[int, str] | None:
    """(revision, label id) after the upsert, or None when the target is not the owner's. With tracing on, the
    outbox `obs.label` key row is written in the same transaction."""
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
    "SELECT owner_id, target, target_id, ok, reason, revision, assessment_id FROM owner_labels WHERE id = :id"
)
# The traced job of the labelled thing: the lesson's newest compile run's revise stage, or the question's
# answer.
LESSON_JOB_SQL: Final = (
    "SELECT k.job_id, k.key FROM job_keys k WHERE k.kind = 'pipeline.stage' AND k.job_id IS NOT NULL"
    " AND k.args->>'stage' = 'revise' AND k.args->>'run_id' = (SELECT r.id::text FROM compile_runs r"
    " WHERE r.lesson_id = :tid AND r.owner_id = :o ORDER BY r.created_at DESC LIMIT 1)"
    " ORDER BY k.created_at DESC LIMIT 1"
)
ANSWER_JOB_SQL: Final = (
    "SELECT k.job_id, k.key FROM job_keys k WHERE k.kind = 'tutor.answer' AND k.job_id IS NOT NULL"
    " AND k.args->>'question_id' = CAST(:tid AS text) ORDER BY k.created_at DESC LIMIT 1"
)
SAVE_SQL: Final = "UPDATE owner_labels SET assessment_id = :a WHERE id = :id AND revision = :r"


def trace_ref(job_id: int, key: str) -> str:
    from reader.obs.ids import derive_trace_id

    return f"tr-{derive_trace_id(job_id, key):032x}"


async def handle(args: LabelArgs, conn: Any, ctx: Any) -> None:
    from reader.obs import setup as obs_setup

    if not obs_setup.current_settings().enabled:
        return
    row = (await conn.execute(text(ROW_SQL), {"id": args.label_id})).first()
    if row is None:
        return
    job = (
        await conn.execute(
            text(LESSON_JOB_SQL if row.target == "lesson" else ANSWER_JOB_SQL),
            {"tid": str(row.target_id), "o": row.owner_id},
        )
    ).first()
    if job is None:
        log.info("obs label no_trace target=%s", row.target)
        return
    label = Label(
        target=row.target,
        target_id=str(row.target_id),
        ok=bool(row.ok),
        reason=row.reason,
        revision=int(row.revision),
        assessment_id=row.assessment_id,
    )
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            got = await mirror(
                label,
                job_revision=args.revision,
                trace_ref=trace_ref(int(job.job_id), job.key),
                client=client,
            )
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
    max_attempts=3,
    transient=(httpx.TransportError, httpx.HTTPStatusError),
    timeout_s=30,
)
