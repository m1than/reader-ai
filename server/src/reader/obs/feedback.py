"""Code-sourced assessments (SRV-OBS FR-OBS-14, ADR-0035): job kind `obs.feedback`. The body of every request
comes only from the catalogue of VEC-OBS-4 (`reader.obs.analytics.ASSESSMENTS`); the code that computes an
outcome calls `record`, which enqueues the job and never raises. The handler looks for an existing assessment
of the same name and source before it posts, so a retry or a lost response never adds a second one."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Final

import httpx
from pydantic import BaseModel, ConfigDict, Field

from reader.jobs.model import JobKind
from reader.obs.analytics import ASSESSMENTS
from reader.obs.ids import derive_trace_id

log = logging.getLogger("reader.obs")
KIND: Final = "obs.feedback"
MLFLOW: Final = "http://mlflow:5000"
MAX_ATTEMPTS: Final = 3
SCHEDULE_IN_S: Final = 60
HTTP_TIMEOUT_S: Final = 10.0


class CatalogError(ValueError):
    """A name, value or metadata key outside VEC-OBS-4's catalogue: no request is built."""


class FeedbackRefused(Exception):
    """MLflow answered 4xx other than 404: the key ends dead with this class name."""


class TraceMissing(Exception):
    """MLflow answered 404 (the trace is not ingested yet): transient while attempts remain."""


class FeedbackArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: int = Field(ge=1)
    key: str = Field(min_length=1, max_length=512)
    name: str = Field(max_length=64)
    value: bool | float
    metadata: dict[str, int] = Field(default_factory=dict)


def checked(name: str, value: object, metadata: Mapping[str, object]) -> tuple[bool | float, dict[str, str]]:
    """The value after its rule and the metadata as decimal strings, or CatalogError."""
    entry = ASSESSMENTS.get(name)
    if entry is None:
        raise CatalogError("name")
    rule = entry["value"]
    if rule == "bool":
        if not isinstance(value, bool):
            raise CatalogError("value")
        kept: bool | float = value
    else:
        _, low, high = rule
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= float(value) <= high:
            raise CatalogError("value")
        kept = float(value)
    allowed: Mapping[str, tuple[int, int]] = entry["metadata"]
    out: dict[str, str] = {}
    for key, raw in metadata.items():
        bounds = allowed.get(key)
        if bounds is None or isinstance(raw, bool) or not isinstance(raw, int):
            raise CatalogError("metadata")
        if not bounds[0] <= raw <= bounds[1]:
            raise CatalogError("metadata")
        out[key] = str(raw)
    return kept, out


def body(name: str, value: object, metadata: Mapping[str, object], trace_id: str) -> dict[str, Any]:
    """The CreateAssessment body: catalogue fields only, source CODE, no rationale (FR-OBS-14)."""
    kept, meta = checked(name, value, metadata)
    return {"assessment": {
        "assessment_name": name,
        "trace_id": trace_id,
        "source": {"source_type": "CODE", "source_id": ASSESSMENTS[name]["source_id"]},
        "feedback": {"value": kept},
        "metadata": meta,
    }}


def trace_ref(job_id: int, key: str) -> str:
    return f"tr-{derive_trace_id(job_id, key):032x}"


async def record(ctx: Any, name: str, value: bool | float, metadata: Mapping[str, int] | None = None) -> None:
    """Enqueue `obs.feedback` for the running job's trace; a failure is logged and changes nothing."""
    try:
        from reader.jobs import running

        checked(name, value, metadata or {})
        args = {"job_id": int(ctx.job_id), "key": str(ctx.key), "name": name, "value": value,
                "metadata": dict(metadata or {})}
        await running().enqueue(KIND, args, schedule_in=SCHEDULE_IN_S)
    except Exception:
        log.warning("obs feedback_enqueue_failed name=%s", name if name in ASSESSMENTS else "<invalid>")


async def post(args: FeedbackArgs, client: httpx.AsyncClient, last: bool) -> bool:
    """True when an assessment was created; False when one existed or the trace never arrived (last try)."""
    trace_id = trace_ref(args.job_id, args.key)
    payload = body(args.name, args.value, args.metadata, trace_id)
    source_id = payload["assessment"]["source"]["source_id"]
    got = await client.get(f"{MLFLOW}/ajax-api/3.0/mlflow/traces/get", params={"trace_id": trace_id},
                           timeout=HTTP_TIMEOUT_S)
    if got.status_code == 404:
        return _missing(args.name, last)
    _raise_for(got)
    info = ((got.json() or {}).get("trace") or {}).get("trace_info") or {}
    for existing in info.get("assessments") or []:
        same_source = (existing.get("source") or {}).get("source_id") == source_id
        if existing.get("assessment_name") == args.name and same_source:
            return False
    sent = await client.post(f"{MLFLOW}/ajax-api/3.0/mlflow/traces/{trace_id}/assessments", json=payload,
                             timeout=HTTP_TIMEOUT_S)
    if sent.status_code == 404:
        return _missing(args.name, last)
    _raise_for(sent)
    return True


def _missing(name: str, last: bool) -> bool:
    if not last:
        raise TraceMissing(name)
    log.info("obs feedback_no_trace name=%s", name)
    return False


def _raise_for(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    if response.status_code < 500:
        raise FeedbackRefused(str(response.status_code))
    raise httpx.HTTPStatusError("mlflow", request=response.request, response=response)


async def handle(args: FeedbackArgs, conn: Any, ctx: Any) -> None:
    from reader.obs import setup as obs_setup

    if not obs_setup.current_settings().enabled:
        return
    last = int(getattr(ctx, "attempts", 0)) + 1 >= MAX_ATTEMPTS
    async with httpx.AsyncClient(trust_env=False) as client:
        await post(args, client, last)


KIND_DECL: Final = JobKind(
    name=KIND,
    queue="obs",
    args=FeedbackArgs,
    key=("job_id", "name"),
    handler=handle,
    max_attempts=MAX_ATTEMPTS,
    transient=(TraceMissing, httpx.TransportError, httpx.HTTPStatusError),
    timeout_s=30,
)
