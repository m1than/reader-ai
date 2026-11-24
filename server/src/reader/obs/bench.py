"""SRV-OBS FR-OBS-22, FR-OBS-23 (ADR-0038): the owner's bench upload, its MLflow dataset and evaluation Run.

An upload carries a set file and a result. The set must be pinned in VEC-OBS-5 by its sha256; the result's
required `kind` must equal the set's; the result must pass VEC-OBS-5 `bench_results` exactly (unknown keys
refused, so no prose can enter); every per-question id must be a registered record of the set. Scores are
recomputed by code over the registered records only; the file's own aggregates are validated but never stored
(R4). The route never starts a bench: it records results. `obs.bench` then writes the dataset and one Run in
experiment `bench`."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import httpx
from pydantic import BaseModel, ConfigDict, Field
from ruamel.yaml import YAML
from sqlalchemy import text

from reader.jobs.model import JobKind
from reader.obs import phase2
from reader.obs.phase2 import MlflowRefused, request

log = logging.getLogger("reader.obs")
KIND: Final = "obs.bench"
EXPERIMENT_NAME: Final = "bench"
MAX_BYTES: Final = int(phase2.VEC["upload"]["max_bytes"])


class BenchError(Exception):
    """A refused upload: `code` is the route's error code (set_not_pinned | bench_invalid)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ObsBenchRefused(Exception):
    """MLflow answered 4xx: the key ends dead with this class name."""


@dataclass(frozen=True)
class Upload:
    kind: str
    set_name: str
    set_sha256: str
    model: str | None
    commit: str | None
    params: dict[str, Any]
    scores: dict[str, float]
    per_question: list[dict[str, Any]]
    records: list[dict[str, Any]]
    digest: str


def _sha(textual: str) -> str:
    return hashlib.sha256(textual.encode("utf-8")).hexdigest()


def pinned(set_text: str) -> tuple[str, str] | None:
    sha = _sha(set_text)
    for name, entry in phase2.VEC["bench_sets"].items():
        if entry["sha256"] == sha:
            return str(name), str(entry["kind"])
    return None


def _questions(set_text: str) -> list[dict[str, Any]]:
    loaded = YAML(typ="safe").load(set_text) or {}
    qs = loaded.get("questions") if isinstance(loaded, dict) else None
    return [q for q in qs or [] if isinstance(q, dict)]


def _registered(set_text: str) -> list[dict[str, Any]]:
    found = pinned(set_text)
    if found is None:
        return []
    entry = phase2.VEC["bench_sets"][found[0]]
    excluded = set(entry.get("exclude") or [])
    return [q for q in _questions(set_text) if q.get("id") not in excluded]


def dataset_records(set_text: str) -> list[dict[str, Any]]:
    """VEC-OBS-5 `dataset_record`: inputs {question_id, question, chapter}, expectations {gold_pages}."""
    return [
        {
            "inputs": {
                "question_id": str(q["id"]),
                "question": str(q["question"]),
                "chapter": int(q["chapter"]),
            },
            "expectations": {"gold_pages": [int(p) for p in q["gold_pages"]]},
        }
        for q in _registered(set_text)
    ]


# --- the VEC-OBS-5 rule checker for uploads ---


def _ok(value: Any, rule: Any, schema: Mapping[str, Any]) -> bool:
    if isinstance(rule, str):
        if rule == "bool":
            return isinstance(value, bool)
        if rule in schema:
            return _object(value, schema[rule], schema)
        return phase2.check(value, rule)
    if not isinstance(rule, Mapping):
        return False
    if "list" in rule:
        if (
            not isinstance(value, list)
            or len(value) > int(rule.get("max", 10**9))
            or len(value) < int(rule.get("min", 0))
        ):
            return False
        return all(_ok(v, rule["list"], schema) for v in value)
    if "int_or_null" in rule:
        return value is None or _ok(value, {"int": rule["int_or_null"]}, schema)
    if "enum_or_null" in rule:
        options = rule["enum_or_null"]
        return value is None or (isinstance(options, list) and value in options)
    if "uuid_or_null" in rule:
        return value is None or phase2.check(value, "uuid")
    if any(k in rule for k in ("enum", "int", "float", "enum_from")):
        return phase2.check(value, rule)
    return _object(value, rule, schema)  # a nested object of rules


def _object(
    value: Any, rules: Mapping[str, Any], schema: Mapping[str, Any], optional: Mapping[str, Any] | None = None
) -> bool:
    if not isinstance(value, dict):
        return False
    allowed = {**rules, **(optional or {})}
    if set(value) - set(allowed):
        return False  # unknown keys are refused (no prose)
    if set(rules) - set(value):
        return False
    return all(_ok(value[k], allowed[k], schema) for k in value)


def _nearest(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return float(ordered[max(0, math.ceil(q * len(ordered)) - 1)])


def _scores_search(rows: list[dict[str, Any]]) -> dict[str, float]:
    n = len(rows)
    ranks = [r.get("rank") for r in rows]
    return {
        "hit_at_1": sum(1 for r in ranks if isinstance(r, int) and r <= 1) / n,
        "hit_at_3": sum(1 for r in ranks if isinstance(r, int) and r <= 3) / n,
        "hit_at_10": sum(1 for r in ranks if isinstance(r, int) and r <= 10) / n,
        "reciprocal_rank": sum(1.0 / r for r in ranks if isinstance(r, int) and r > 0) / n,
    }


def _scores_tutor(rows: list[dict[str, Any]]) -> dict[str, float]:
    n = len(rows)
    costed = [
        r
        for r in rows
        if r.get("state") == "answered" or (r.get("state") == "expired" and r.get("usd", 0) > 0)
    ]
    walls = [float(r["wall_s"]) for r in rows]
    out = {
        "grounded_share": sum(1 for r in rows if r["grounded"]) / n,
        "gold_cited_share": sum(1 for r in rows if r["gold_cited"]) / n,
        "insufficient_share": sum(1 for r in rows if r["insufficient"]) / n,
        "total_usd": float(sum(float(r["usd"]) for r in rows)),
    }
    for name, value in (
        ("latency_p50_s", _nearest(walls, 0.5)),
        ("latency_p95_s", _nearest(walls, 0.95)),
        ("mean_usd", (sum(float(r["usd"]) for r in costed) / len(costed)) if costed else None),
    ):
        if value is not None:
            out[name] = float(value)
    return out


def validate(set_text: str, result: Any) -> Upload:
    found = pinned(set_text)
    if found is None:
        raise BenchError("set_not_pinned")
    set_name, kind = found
    spec = phase2.VEC["bench_results"][kind]
    if not isinstance(result, dict) or result.get("kind") != kind:
        raise BenchError("bench_invalid")
    if not _object(result, spec["required"], spec, spec.get("optional")):
        raise BenchError("bench_invalid")
    registered = {str(q["id"]) for q in _registered(set_text)}
    rows = result["per_question"] if kind == "search" else result["records"]
    if not rows or any(r["id"] not in registered for r in rows):
        raise BenchError("bench_invalid")
    scores = _scores_search(rows) if kind == "search" else _scores_tutor(rows)
    params = (
        {"model": result["model"], "chunk_chars": result["chunk_chars"], "overlap": result["overlap"]}
        if kind == "search"
        else {"lesson_id": result["lesson_id"], "date": result["date"]}
    )
    per_question = [{"id": r["id"], "rank": r.get("rank")} for r in rows] if kind == "search" else []
    digest = _sha(
        json.dumps({"set": _sha(set_text), "result": result}, sort_keys=True, separators=(",", ":"))
    )
    return Upload(
        kind=kind,
        set_name=set_name,
        set_sha256=_sha(set_text),
        model=result.get("model") if kind == "search" else None,
        commit=result.get("commit"),
        params=params,
        scores=scores,
        per_question=per_question,
        records=dataset_records(set_text),
        digest=digest,
    )


# --- the route's record (FR-OBS-23: Postgres is the record; the outbox job in the same transaction) ---

INSERT_SQL: Final = (
    "INSERT INTO obs_bench_results (id, owner_id, digest, kind, set_name, set_sha256, model,"
    ' "commit", params, scores, per_question, records) VALUES (:id, :o, :d, :k, :s, :ss, :m, :c,'
    " CAST(:p AS jsonb), CAST(:sc AS jsonb), CAST(:pq AS jsonb), CAST(:r AS jsonb))"
    " ON CONFLICT (digest) DO NOTHING RETURNING id"
)


async def record(conn: Any, owner: uuid.UUID, upload: Upload) -> uuid.UUID | None:
    """The row (None when the same digest exists) and the outbox `obs.bench` key in the same transaction."""
    upload_id = uuid.uuid4()
    got = (
        await conn.execute(
            text(INSERT_SQL),
            {
                "id": upload_id,
                "o": owner,
                "d": upload.digest,
                "k": upload.kind,
                "s": upload.set_name,
                "ss": upload.set_sha256,
                "m": upload.model,
                "c": upload.commit,
                "p": json.dumps(upload.params),
                "sc": json.dumps(upload.scores),
                "pq": json.dumps(upload.per_question),
                "r": json.dumps(upload.records),
            },
        )
    ).first()
    if got is None:
        return None
    await phase2.stage(conn, KIND_DECL, {"upload_id": str(upload_id)})
    return upload_id


# --- the job ---


class BenchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    upload_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


ROW_SQL: Final = (
    'SELECT kind, set_name, set_sha256, model, "commit", params, scores, per_question, records, created_at'
    " FROM obs_bench_results WHERE id = :id"
)


async def _bench_experiment(client: httpx.AsyncClient) -> str:
    for attempt in (1, 2):
        status, got = await request(
            client,
            "GET",
            "/api/2.0/mlflow/experiments/get-by-name",
            params={"experiment_name": EXPERIMENT_NAME},
            ok404=True,
        )
        if status != 404:
            return str((got.get("experiment") or {})["experiment_id"])
        if attempt == 1:
            from reader.obs.bootstrap import ensure_inline

            await ensure_inline(client)  # FR-OBS-26 (2): idempotent, reads first
    raise ObsBenchRefused("no bench experiment")


async def write(client: httpx.AsyncClient, upload_id: str, row: Mapping[str, Any]) -> str:
    experiment = await _bench_experiment(client)
    _, found = await request(
        client,
        "POST",
        "/api/3.0/mlflow/datasets/search",
        {"experiment_ids": [experiment], "filter_string": f"name = '{row['set_name']}'"},
    )
    datasets = [d for d in found.get("datasets") or [] if isinstance(d, dict)]
    if datasets:
        dataset = datasets[0]
    else:
        _, made = await request(
            client,
            "POST",
            "/api/3.0/mlflow/datasets/create",
            {
                "name": row["set_name"],
                "experiment_ids": [experiment],
                "tags": json.dumps({"reader.source_sha256": row["set_sha256"], "reader.bench": row["kind"]}),
            },
        )
        dataset = made.get("dataset") or {}
    dataset_id = str(dataset["dataset_id"])
    await request(
        client,
        "POST",
        f"/api/3.0/mlflow/datasets/{dataset_id}/records",
        {"records": json.dumps(row["records"])},
    )
    _, runs = await request(
        client,
        "POST",
        "/api/2.0/mlflow/runs/search",
        {
            "experiment_ids": [experiment],
            "filter": f"tags.`reader.bench_upload` = '{upload_id}'",
            "max_results": 2,
        },
    )
    listed = [r for r in runs.get("runs") or [] if isinstance(r, dict)]
    created_ms = int(row["created_at"].timestamp() * 1000)
    if listed:
        run = listed[0]
        tags = {t["key"]: t["value"] for t in (run.get("data") or {}).get("tags") or []}
        if tags.get("reader.complete") == "true":
            return str(run["info"]["run_id"])
        run_id = str(run["info"]["run_id"])
        have = {p["key"] for p in (run.get("data") or {}).get("params") or []}
    else:
        _, made_run = await request(
            client,
            "POST",
            "/api/2.0/mlflow/runs/create",
            {
                "experiment_id": experiment,
                "run_name": f"{row['kind']} bench {row['set_name']}",
                "start_time": created_ms,
                "tags": [
                    {"key": "reader.bench", "value": row["kind"]},
                    {"key": "reader.bench_upload", "value": upload_id},
                ],
            },
        )
        run_id = str(made_run["run"]["info"]["run_id"])
        have = set()
    params = {**{k: str(v) for k, v in (row["params"] or {}).items()}, "set_sha256": row["set_sha256"]}
    if row.get("commit"):
        params["commit"] = str(row["commit"])
    metrics = [
        {"key": k, "value": float(v), "timestamp": created_ms, "step": 0} for k, v in row["scores"].items()
    ]
    metrics += [
        {"key": "rank", "value": float(q["rank"] or 0), "timestamp": created_ms, "step": i}
        for i, q in enumerate(row["per_question"] or [])
    ]
    await request(
        client,
        "POST",
        "/api/2.0/mlflow/runs/log-batch",
        {
            "run_id": run_id,
            "params": [{"key": k, "value": v} for k, v in params.items() if k not in have],
            "metrics": metrics,
            "tags": [],
        },
    )
    await request(
        client,
        "POST",
        "/api/2.0/mlflow/runs/log-inputs",
        {
            "run_id": run_id,
            "datasets": [
                {
                    "tags": [{"key": "mlflow.data.context", "value": "evaluation"}],
                    "dataset": {
                        "name": row["set_name"],
                        "digest": str(dataset.get("digest") or row["set_sha256"][:8]),
                        "source_type": "mlflow_evaluation_dataset",
                        "source": json.dumps({"dataset_id": dataset_id}),
                    },
                }
            ],
        },
    )
    await request(
        client,
        "POST",
        "/api/2.0/mlflow/runs/update",
        {"run_id": run_id, "status": "FINISHED", "end_time": created_ms},
    )
    await request(
        client,
        "POST",
        "/api/2.0/mlflow/runs/set-tag",
        {"run_id": run_id, "key": "reader.complete", "value": "true"},
    )
    return run_id


async def handle(args: BenchArgs, conn: Any, ctx: Any) -> None:
    from reader.obs import setup as obs_setup

    if not obs_setup.current_settings().enabled:
        return
    row = (await conn.execute(text(ROW_SQL), {"id": args.upload_id})).mappings().first()
    if row is None:
        return
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            await write(client, args.upload_id, dict(row))
    except MlflowRefused as refused:
        raise ObsBenchRefused(str(refused.status)) from None


KIND_DECL: Final = JobKind(
    name=KIND,
    queue="obs",
    args=BenchArgs,
    key=("upload_id",),
    handler=handle,
    max_attempts=3,
    transient=(httpx.TransportError, httpx.HTTPStatusError),
    timeout_s=60,
)
