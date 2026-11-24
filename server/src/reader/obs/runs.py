"""SRV-OBS FR-OBS-20 (ADR-0038): one MLflow Run per compile run and per import, in experiment 0 beside their
traces; and FR-OBS-31's retention of Runs and LoggedModels.

`sync` writes one end of a run from a `Snapshot` the handler read from Postgres once (never refreshed): it
finds the Run by its id tag (two → ObsRunsDuplicate), creates it if missing, logs only the params the Run
lacks (params are write-once in MLflow), the metrics at step = end and the tags, links the run's traces in
batches of at most 100, ends the Run and tags `reader.complete` LAST, so an incomplete Run is completed by
the next attempt, never duplicated. Every value passes VEC-OBS-5 `runs` before any request (INV-OBS-2's
phase 2 clause)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from reader.jobs.model import JobKind, lock_id
from reader.obs import phase2
from reader.obs.phase2 import MlflowRefused, request

log = logging.getLogger("reader.obs")
KIND: Final = "obs.runs"
SCHEDULE_IN_S: Final = 60
LINK_BATCH: Final = 100
TRACE_PAGE: Final = 500
EXPERIMENT: Final = "0"
ID_TAG: Final = {"compile": "reader.run_id", "ingest": "reader.ingest_id"}


class ObsRunsDuplicate(Exception):
    """Two Runs carry one id tag: only a hand-made Run or a job without the lock can cause it (dead)."""


class ObsRunsRefused(Exception):
    """MLflow answered 4xx: the key ends dead with this class name."""


class RunsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["compile", "ingest"]
    id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    end: int = Field(ge=1, le=100)


@dataclass(frozen=True)
class Snapshot:
    kind: str
    key: str
    name: str
    start_ms: int
    end_ms: int
    status: str  # FINISHED | FAILED
    outcome: str
    end: int
    tags: Mapping[str, str]
    params: Mapping[str, str]
    metrics: Mapping[str, float | int]
    snapshot_at: str
    trace_tag: tuple[str, str] | None = field(default=None)


def _rule(table: Mapping[str, Any], key: str) -> Any:
    if key in table:
        return table[key]
    for name, rule in table.items():
        prefix = name.split("<", 1)[0]
        if "<" in name and key.startswith(prefix) and phase2.check(key[len(prefix) :], "stage"):
            return rule  # "<stage>": a declared SRV-PIPELINE stage only
    return None


_OUTCOMES: Final = frozenset(
    v for kind in ("compile", "ingest") for v in phase2.VEC["runs"][kind]["tags"]["reader.outcome"]["enum"]
)


def _validate(s: Snapshot) -> None:
    spec = phase2.VEC["runs"][s.kind]
    for k, v in s.tags.items():
        if not phase2.check(v, _rule(spec["tags"], k)):
            raise ValueError(f"run tag {k}")
    if s.outcome not in _OUTCOMES or not phase2.check(s.snapshot_at, "datetime"):
        raise ValueError("run outcome or snapshot")
    if not phase2.check(s.end, phase2.VEC["runs"]["compile"]["tags"]["reader.ends"]):
        raise ValueError("run end")
    for k, param in s.params.items():
        if not phase2.check(param, _rule(spec["params"], k)):
            raise ValueError(f"run param {k}")
    for k, metric in s.metrics.items():
        if not phase2.check(metric, _rule(spec["metrics"], k)):
            raise ValueError(f"run metric {k}")
    if s.status not in ("FINISHED", "FAILED"):
        raise ValueError("run status")


async def _find(client: httpx.AsyncClient, s: Snapshot) -> dict[str, Any] | None:
    _, got = await request(
        client,
        "POST",
        "/api/2.0/mlflow/runs/search",
        {"experiment_ids": [EXPERIMENT], "filter": f"tags.`{ID_TAG[s.kind]}` = '{s.key}'", "max_results": 2},
    )
    found = [r for r in got.get("runs") or [] if isinstance(r, dict)]
    if len(found) > 1:
        raise ObsRunsDuplicate(s.kind)
    return found[0] if found else None


def _kv(items: Any) -> dict[str, str]:
    return {str(i["key"]): str(i.get("value", "")) for i in items or [] if isinstance(i, dict) and "key" in i}


async def _trace_ids(client: httpx.AsyncClient, s: Snapshot) -> list[str]:
    tag, value = s.trace_tag or (ID_TAG[s.kind], s.key)
    ids: list[str] = []
    token: str | None = None
    while True:
        body: dict[str, Any] = {
            "locations": [{"type": "MLFLOW_EXPERIMENT", "mlflow_experiment": {"experiment_id": EXPERIMENT}}],
            "filter": f"tag.`{tag}` = '{value}'",
            "max_results": TRACE_PAGE,
        }
        if token:
            body["page_token"] = token
        _, got = await request(client, "POST", "/ajax-api/3.0/mlflow/traces/search", body)
        ids += [t["trace_id"] for t in got.get("traces") or [] if isinstance(t, dict) and "trace_id" in t]
        token = got.get("next_page_token") or None
        if not token:
            return ids


async def sync(s: Snapshot, client: httpx.AsyncClient) -> str:
    """One end of a run into its Run; returns the run id."""
    _validate(s)
    found = await _find(client, s)
    if found is None:
        _, made = await request(
            client,
            "POST",
            "/api/2.0/mlflow/runs/create",
            {
                "experiment_id": EXPERIMENT,
                "run_name": s.name,
                "start_time": s.start_ms,
                "tags": [{"key": "reader.kind", "value": s.kind}, {"key": ID_TAG[s.kind], "value": s.key}],
            },
        )
        run_id = str(made["run"]["info"]["run_id"])
        have: dict[str, str] = {}
    else:
        run_id = str(found["info"]["run_id"])
        have = _kv((found.get("data") or {}).get("params"))
    tags = {
        **s.tags,
        "reader.outcome": s.outcome,
        "reader.ends": str(s.end),
        "reader.snapshot_at": s.snapshot_at,
    }
    await request(
        client,
        "POST",
        "/api/2.0/mlflow/runs/log-batch",
        {
            "run_id": run_id,
            "params": [{"key": k, "value": str(v)} for k, v in s.params.items() if k not in have],
            "metrics": [
                {"key": k, "value": float(v), "timestamp": s.end_ms, "step": s.end}
                for k, v in s.metrics.items()
            ],
            "tags": [{"key": k, "value": str(v)} for k, v in tags.items()],
        },
    )
    ids = await _trace_ids(client, s)
    for start in range(0, len(ids), LINK_BATCH):
        await request(
            client,
            "POST",
            "/api/2.0/mlflow/traces/link-to-run",
            {"trace_ids": ids[start : start + LINK_BATCH], "run_id": run_id},
        )
    await request(
        client,
        "POST",
        "/api/2.0/mlflow/runs/update",
        {"run_id": run_id, "status": s.status, "end_time": s.end_ms},
    )
    await request(
        client,
        "POST",
        "/api/2.0/mlflow/runs/set-tag",
        {"run_id": run_id, "key": "reader.complete", "value": "true"},
    )
    return run_id


async def enqueue_end(kind: str, ident: str, end: int) -> None:
    """FR-PIPELINE-18, FR-INGEST-26: called after the end committed; never raises (INV-OBS-3)."""
    try:
        if not phase2.tracing_on():
            return
        from reader.jobs import running

        await running().enqueue(
            KIND, {"kind": kind, "id": str(ident), "end": int(end)}, schedule_in=SCHEDULE_IN_S
        )
    except Exception:
        log.warning("obs runs_enqueue_failed kind=%s", kind if kind in ID_TAG else "<invalid>")


# --- the snapshot from Postgres (read once per job) ---

_COMPILE_SQL: Final = (
    "SELECT r.id, r.lesson_id, l.course_id, r.created_at, r.finished_at, r.loops FROM compile_runs r"
    " JOIN lessons l ON l.id = r.lesson_id AND l.owner_id = r.owner_id WHERE r.id = :id"
)
_SPEND_SQL: Final = (
    "SELECT s.stage, sum(CASE WHEN s.kind = 'settle' THEN s.settled_usd WHEN s.kind = 'reserve'"
    " AND NOT EXISTS (SELECT 1 FROM agent_spend x WHERE x.reservation_id = s.id) THEN s.usd ELSE 0 END)"
    " AS usd,"
    " count(*) FILTER (WHERE s.kind = 'reserve' AND NOT EXISTS (SELECT 1 FROM agent_spend x"
    " WHERE x.reservation_id = s.id)) AS open, coalesce(sum(s.input), 0) AS tin,"
    " coalesce(sum(s.output), 0) AS tout"
    " FROM agent_spend s WHERE s.run_id = :id GROUP BY s.stage"
)
_MODELS_SQL: Final = "SELECT stage, max(model) AS model FROM compile_steps WHERE run_id = :id GROUP BY stage"
_INGEST_SQL: Final = (
    "SELECT i.id, i.converter_version, i.span_count, i.imported_at, m.sha256, m.page_count FROM ingests i"
    " JOIN materials m ON m.id = i.material_id WHERE i.id = :id"
)


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def snapshot(conn: Any, args: RunsArgs, now: datetime) -> Snapshot | None:
    from reader.obs import _STATE
    from reader.obs.export import reader_version

    version = _STATE.get("version_key")
    common_params: dict[str, str] = {"reader_version": reader_version()}
    if isinstance(version, str):
        common_params["version_key"] = version
    if args.kind == "compile":
        row = (await conn.execute(text(_COMPILE_SQL), {"id": args.id})).first()
        if row is None:
            return None
        spend = (await conn.execute(text(_SPEND_SQL), {"id": args.id})).all()
        metrics: dict[str, float | int] = {}
        total, opened, tin, tout = Decimal(0), 0, 0, 0
        for stage, usd, open_count, t_in, t_out in spend:
            amount = Decimal(usd or 0)
            total += amount
            opened += int(open_count or 0)
            tin, tout = tin + int(t_in or 0), tout + int(t_out or 0)
            if isinstance(stage, str) and stage:
                metrics[f"cost_usd.{stage}"] = float(amount)
        ended = row.finished_at or now
        metrics.update(
            {
                "cost_usd.total": float(total),
                "open_reserves": opened,
                "tokens.input": tin,
                "tokens.output": tout,
                "loops": int(row.loops or 0),
                "duration_s.total": max((ended - row.created_at).total_seconds(), 0.0),
            }
        )
        params = dict(common_params)
        for stage, model in (await conn.execute(text(_MODELS_SQL), {"id": args.id})).all():
            if isinstance(stage, str) and isinstance(model, str):
                params[f"model.{stage}"] = model
        finished = row.finished_at is not None
        return Snapshot(
            kind="compile",
            key=args.id,
            name=f"compile {args.id[:8]}",
            start_ms=_ms(row.created_at),
            end_ms=_ms(ended),
            status="FINISHED" if finished else "FAILED",
            outcome="finished" if finished else "failed",
            end=args.end,
            tags={"reader.lesson.id": str(row.lesson_id), "reader.course.id": str(row.course_id)},
            params=params,
            metrics=metrics,
            snapshot_at=_iso(now),
        )
    row = (await conn.execute(text(_INGEST_SQL), {"id": args.id})).first()
    if row is None:
        return None
    return Snapshot(
        kind="ingest",
        key=args.id,
        name=f"import {row.sha256[:8]}",
        start_ms=_ms(row.imported_at),
        end_ms=_ms(row.imported_at),
        status="FINISHED",
        outcome="succeeded",
        end=args.end,
        tags={"reader.material_sha256": row.sha256},
        params={"converter_version": row.converter_version, "material_sha256": row.sha256},
        metrics={"pages": int(row.page_count or 0), "spans": int(row.span_count or 0)},
        snapshot_at=_iso(now),
        trace_tag=("reader.material_sha256", row.sha256),
    )


async def handle(args: RunsArgs, conn: Any, ctx: Any) -> None:
    from reader.obs import setup as obs_setup

    if not obs_setup.current_settings().enabled:
        return
    # Two ends of one run never write the same Run at once (the create race behind ObsRunsDuplicate).
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_id(f"obs.runs:{args.kind}:{args.id}")}
    )
    snap = await snapshot(conn, args, datetime.now(UTC))
    if snap is None:
        return
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            await sync(snap, client)
    except MlflowRefused as refused:
        raise ObsRunsRefused(str(refused.status)) from None


async def prune(
    client: httpx.AsyncClient, *, now: datetime, retention_days: int, running_key: str | None
) -> int:
    """FR-OBS-31: compile and import Runs whose end time is older than retention_days, and LoggedModels whose
    `reader.last_started` is older (never the running worker's key). Returns the number of deletes."""
    cutoff = now - timedelta(days=retention_days)
    cutoff_ms, cutoff_day = _ms(cutoff), cutoff.date().isoformat()
    deleted = 0
    _, got = await request(
        client,
        "POST",
        "/api/2.0/mlflow/runs/search",
        {
            "experiment_ids": [EXPERIMENT],
            "filter": "tags.`reader.kind` IN ('compile', 'ingest')",
            "max_results": 1000,
        },
    )
    for run in got.get("runs") or []:
        info = run.get("info") or {}
        end = info.get("end_time")
        if isinstance(end, int) and end < cutoff_ms:
            await request(client, "POST", "/api/2.0/mlflow/runs/delete", {"run_id": info["run_id"]})
            deleted += 1
    _, models = await request(
        client,
        "POST",
        "/api/2.0/mlflow/logged-models/search",
        {"experiment_ids": [EXPERIMENT], "filter": "name = 'reader-agent'"},
    )
    for model in models.get("models") or []:
        info = model.get("info") or {}
        tags = _kv(info.get("tags"))
        last = tags.get("reader.last_started", "")
        if tags.get("reader.version_key") == running_key or not last or last >= cutoff_day:
            continue
        await request(client, "DELETE", f"/api/2.0/mlflow/logged-models/{info['model_id']}")
        deleted += 1
    return deleted


KIND_DECL: Final = JobKind(
    name=KIND,
    queue="obs",
    args=RunsArgs,
    key=("kind", "id", "end"),
    handler=handle,
    max_attempts=3,
    transient=(httpx.TransportError, httpx.HTTPStatusError),
    timeout_s=60,
)
