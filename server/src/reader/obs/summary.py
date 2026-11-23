"""The owner's views behind GET /api/obs/summary (SRV-OBS FR-OBS-10), computed from reader-ai's own tables.

User-owned tables are read with the caller's owner id; `job_keys` only through SRV-EVENTS' published reads
(FR-EVENTS-17, -18), with keys computed by `idempotency_key` from the owner's own rows. No text column is
read. Every trace id is FR-OBS-5's derivation from a key row's job id and key.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from reader.obs.ids import derive_trace_id

RUNS_LIMIT: Final = 50
INGEST_LIMIT: Final = 50
SLOWEST: Final = 10
TRACE_IDS: Final = 5
READ_LIMIT: Final = 500
TUTOR_TASK: Final = "tutor"
SYSTEM_KINDS: Final = frozenset({"search.embed", "obs.retention"})


def nearest_rank(values: Sequence[int], p: int) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil(p / 100 * len(ordered))
    return ordered[max(rank, 1) - 1]


def _usd(value: Decimal | None) -> float:
    return float(round(Decimal(value or 0), 6))


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _trace(row: Any) -> str | None:
    return None if row is None or row.job_id is None else format(derive_trace_id(row.job_id, row.key), "032x")


async def _rows(conn: AsyncConnection, sql: str, **params: Any) -> list[Any]:
    return list((await conn.execute(text(sql), params)).all())


def _kinds() -> Any:
    from reader.jobs.catalog import load_kinds

    return load_kinds()


def _key(catalog: Any, kind: str, args: Mapping[str, Any]) -> str:
    from reader.jobs.model import idempotency_key

    return str(idempotency_key(catalog[kind], args))


async def _key_rows(conn: AsyncConnection, keys: Iterable[str]) -> dict[str, Any]:
    from reader.jobs.queue import key_rows_of

    wanted = list(dict.fromkeys(keys))
    return {r.key: r for r in await key_rows_of(conn, wanted)} if wanted else {}


async def _compile_runs(conn: AsyncConnection, owner: uuid.UUID, since: datetime, catalog: Any) -> list[Any]:
    from reader.pipeline.runs import kind_for

    runs = await _rows(
        conn,
        "SELECT id, lesson_id, created_at, finished_at FROM compile_runs"
        " WHERE owner_id = :o AND created_at >= :s ORDER BY created_at DESC, id LIMIT :n",
        o=owner, s=since, n=RUNS_LIMIT,
    )
    ids = [r.id for r in runs]
    if not ids:
        return []
    spent = dict(await _rows(
        conn,
        "SELECT s.run_id, coalesce(sum(s.settled_usd) FILTER (WHERE s.kind = 'settle'), 0)"
        " + coalesce(sum(s.usd) FILTER (WHERE s.kind = 'reserve' AND NOT EXISTS"
        " (SELECT 1 FROM agent_spend c WHERE c.reservation_id = s.id)), 0)"
        " FROM agent_spend s WHERE s.owner_id = :o AND s.run_id = ANY(:ids) GROUP BY s.run_id",
        o=owner, ids=ids,
    ))
    steps = await _rows(
        conn,
        "SELECT run_id, step, stage, input_sha256, cost_usd, duration_s, model FROM compile_steps"
        " WHERE owner_id = :o AND run_id = ANY(:ids) ORDER BY run_id, step",
        o=owner, ids=ids,
    )

    def key_of(s: Any) -> str:
        args = {"run_id": str(s.run_id), "step": s.step, "stage": s.stage, "input_sha256": s.input_sha256}
        return _key(catalog, kind_for(s.stage), args)

    keys = await _key_rows(conn, (key_of(s) for s in steps))
    by_run: dict[Any, list[dict[str, Any]]] = {}
    for s in steps:
        by_run.setdefault(s.run_id, []).append({
            "step": s.step, "stage": s.stage, "cost_usd": _usd(s.cost_usd), "duration_s": float(s.duration_s),
            "model": s.model, "trace_id": _trace(keys.get(key_of(s))),
        })
    return [
        {"run_id": str(r.id), "lesson_id": str(r.lesson_id), "started_at": _iso(r.created_at),
         "finished_at": _iso(r.finished_at), "spent_usd": _usd(spent.get(r.id)),
         "steps": by_run.get(r.id, [])}
        for r in runs
    ]


async def _tutor(conn: AsyncConnection, owner: uuid.UUID, since: datetime, catalog: Any) -> dict[str, Any]:
    answered = await _rows(
        conn,
        "SELECT id, created_at, finished_at FROM tutor_questions WHERE owner_id = :o AND state = 'answered'"
        " AND created_at >= :s AND finished_at IS NOT NULL",
        o=owner, s=since,
    )
    ms = [(int((r.finished_at - r.created_at).total_seconds() * 1000), str(r.id)) for r in answered]
    settles = [v for (v,) in await _rows(
        conn,
        "SELECT settled_usd FROM agent_spend WHERE owner_id = :o AND task = :t AND kind = 'settle'"
        " AND created_at >= :s",
        o=owner, t=TUTOR_TASK, s=since,
    )]
    total = sum((Decimal(v) for v in settles), Decimal(0))
    slowest = sorted(ms, key=lambda m: (-m[0], m[1]))[:SLOWEST]
    keys = {q: _key(catalog, "tutor.answer", {"question_id": q}) for _, q in slowest}
    rows = await _key_rows(conn, keys.values())
    return {
        "count": len(ms),
        "p50_ms": nearest_rank([m for m, _ in ms], 50),
        "p95_ms": nearest_rank([m for m, _ in ms], 95),
        "cost_usd_sum": _usd(total),
        "cost_usd_mean_per_settled_call": _usd(total / len(settles)) if settles else None,
        "slowest": [{"question_id": q, "ms": m, "trace_id": _trace(rows.get(keys[q]))} for m, q in slowest],
    }


async def _failures(
    conn: AsyncConnection, owner: uuid.UUID, since: datetime, catalog: Any, limit: int
) -> dict[str, Any]:
    from reader.jobs.queue import recent_keys_of

    dead, truncated = await recent_keys_of(conn, sorted(catalog), "dead", since, limit)
    runs = {str(r.id) for r in await _rows(conn, "SELECT id FROM compile_runs WHERE owner_id = :o", o=owner)}
    asked = await _rows(conn, "SELECT id FROM tutor_questions WHERE owner_id = :o", o=owner)
    questions = {str(r.id) for r in asked}
    shas = sorted({str(r.args.get("material_sha256")) for r in dead if r.kind == "ingest.import"})
    owners: dict[str, set[str]] = {}
    for sha, who in await _rows(
        conn, "SELECT sha256, owner_id FROM materials WHERE sha256 = ANY(:s)", s=shas
    ):
        owners.setdefault(sha, set()).add(str(who))

    def scope(row: Any) -> bool | None:
        """True: a system row (shown); False: the owner's own row; None: another user's row (hidden)."""
        args = row.args or {}
        if row.kind.startswith("pipeline."):
            return False if str(args.get("run_id")) in runs else None
        if row.kind == "tutor.answer":
            return False if str(args.get("question_id")) in questions else None
        if row.kind == "ingest.import":
            who = owners.get(str(args.get("material_sha256")))
            if not who:
                return True
            return False if str(owner) in who else None
        return True

    groups: dict[tuple[str, str, bool], list[Any]] = {}
    for row in dead:  # newest first
        system = scope(row)
        if system is not None:
            groups.setdefault((row.kind, str(row.error_code), system), []).append(row)
    jobs: list[dict[str, Any]] = [
        {"kind": k, "error_code": c, "count": len(rows), "system": s,
         "trace_ids": [t for t in (_trace(r) for r in rows[:TRACE_IDS]) if t]}
        for (k, c, s), rows in groups.items()
    ]
    jobs.sort(key=lambda g: (-int(g["count"]), str(g["kind"]), str(g["error_code"])))
    failed = await _rows(
        conn,
        "SELECT id, error_code FROM tutor_questions WHERE owner_id = :o AND state = 'failed'"
        " AND created_at >= :s ORDER BY finished_at DESC NULLS LAST, id",
        o=owner, s=since,
    )
    keys = {str(r.id): _key(catalog, "tutor.answer", {"question_id": str(r.id)}) for r in failed}
    key_rows = await _key_rows(conn, keys.values())
    by_code: dict[str, list[str]] = {}
    for r in failed:
        by_code.setdefault(str(r.error_code), []).append(str(r.id))
    tutor: list[dict[str, Any]] = [
        {"error_code": code, "count": len(ids),
         "trace_ids": [t for t in (_trace(key_rows.get(keys[q])) for q in ids[:TRACE_IDS]) if t]}
        for code, ids in by_code.items()
    ]
    tutor.sort(key=lambda g: (-int(g["count"]), str(g["error_code"])))
    return {"truncated": truncated, "jobs": jobs, "tutor": tutor}


async def _ingest(conn: AsyncConnection, owner: uuid.UUID, since: datetime, limit: int) -> dict[str, Any]:
    from reader.jobs.queue import recent_keys_of

    done, truncated = await recent_keys_of(conn, ["ingest.import"], "succeeded", since, limit)
    shas = sorted({str((r.args or {}).get("material_sha256")) for r in done})
    pages = dict(await _rows(
        conn,
        "SELECT sha256, page_count FROM materials WHERE owner_id = :o AND sha256 = ANY(:s)",
        o=owner, s=shas,
    ))
    rows = []
    for r in done:
        sha = str((r.args or {}).get("material_sha256"))
        if sha not in pages or r.finished_at is None:
            continue
        seconds = (r.finished_at - r.created_at).total_seconds()
        rows.append({
            "material_sha256": sha, "page_count": pages[sha], "import_s": seconds,
            "pages_per_min_import": round(pages[sha] / (seconds / 60), 1) if seconds > 0 else None,
            "trace_id": _trace(r),
        })
    return {"truncated": truncated, "rows": rows[:INGEST_LIMIT]}


async def summary(
    conn: AsyncConnection, owner: uuid.UUID, days: int, now: datetime, read_limit: int = READ_LIMIT
) -> dict[str, Any]:
    since = now - timedelta(days=days)
    catalog = _kinds()
    return {
        "compile_runs": await _compile_runs(conn, owner, since, catalog),
        "tutor": await _tutor(conn, owner, since, catalog),
        "failures": await _failures(conn, owner, since, catalog, read_limit),
        "ingest": await _ingest(conn, owner, since, read_limit),
    }
