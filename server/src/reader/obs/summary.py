"""The owner's views behind GET /api/obs/summary (SRV-OBS FR-OBS-10), computed from reader-ai's own tables.

User-owned tables are read with the caller's owner id; `job_keys` only through SRV-EVENTS' published reads
(FR-EVENTS-17, -18), with keys computed by `idempotency_key` from the owner's own rows. No text column is
read. Every trace id is FR-OBS-5's derivation from a key row's job id and key.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
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


@dataclass(frozen=True)
class TraceGate:
    """FR-OBS-10: whether a key row's trace can exist. `periods` are (started_at, enabled) worker starts, in
    any order; a row's moment (finished_at, else created_at) is traced when tracing is on in the api now, the
    latest start at or before it was enabled, and it is not older than `oldest` (retention deletes)."""

    enabled: bool
    periods: tuple[tuple[datetime, bool], ...]
    oldest: datetime

    def traced(self, row: Any) -> bool:
        moment = row.finished_at or row.created_at
        if not self.enabled or moment is None or moment < self.oldest:
            return False
        latest = max(((started, on) for started, on in self.periods if started <= moment), default=None)
        return latest is not None and latest[1]

    def trace_id(self, row: Any) -> str | None:
        """FR-OBS-5's id of the row's newest job, or None when no trace can exist."""
        if row is None or row.job_id is None or not self.traced(row):
            return None
        return format(derive_trace_id(row.job_id, row.key), "032x")


# The starts inside the window plus the one latest before it (the one that decides the window's first rows).
PERIODS_SQL: Final = (
    "SELECT started_at, enabled FROM obs_tracing WHERE started_at >= :s"
    " UNION ALL (SELECT started_at, enabled FROM obs_tracing WHERE started_at < :s"
    " ORDER BY started_at DESC LIMIT 1)"
)


def _uuids(values: Iterable[object]) -> list[uuid.UUID]:
    """The distinct valid UUIDs among `values` (args are validated; an invalid one is skipped, not raised)."""
    found: set[uuid.UUID] = set()
    for value in values:
        try:
            found.add(uuid.UUID(str(value)))
        except ValueError:
            continue
    return sorted(found)


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


async def _compile_runs(
    conn: AsyncConnection, owner: uuid.UUID, since: datetime, catalog: Any, gate: TraceGate
) -> list[Any]:
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
            "model": s.model, "trace_id": gate.trace_id(keys.get(key_of(s))),
        })
    return [
        {"run_id": str(r.id), "lesson_id": str(r.lesson_id), "started_at": _iso(r.created_at),
         "finished_at": _iso(r.finished_at), "spent_usd": _usd(spent.get(r.id)),
         "steps": by_run.get(r.id, [])}
        for r in runs
    ]


async def _tutor(
    conn: AsyncConnection, owner: uuid.UUID, since: datetime, catalog: Any, gate: TraceGate
) -> dict[str, Any]:
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
        "slowest": [
            {"question_id": q, "ms": m, "trace_id": gate.trace_id(rows.get(keys[q]))} for m, q in slowest
        ],
    }


async def _failures(
    conn: AsyncConnection, owner: uuid.UUID, since: datetime, catalog: Any, limit: int, gate: TraceGate
) -> dict[str, Any]:
    from reader.jobs.queue import recent_keys_of

    dead, truncated = await recent_keys_of(conn, sorted(catalog), "dead", since, limit)
    # Ownership of just the ids the dead rows name (at most `limit` each), never every row the owner has.
    run_ids = _uuids(r.args.get("run_id") for r in dead if r.kind.startswith("pipeline."))
    question_ids = _uuids(r.args.get("question_id") for r in dead if r.kind == "tutor.answer")
    runs = {
        str(r.id)
        for r in await _rows(
            conn, "SELECT id FROM compile_runs WHERE owner_id = :o AND id = ANY(:ids)", o=owner, ids=run_ids
        )
    } if run_ids else set()
    questions = {
        str(r.id)
        for r in await _rows(
            conn,
            "SELECT id FROM tutor_questions WHERE owner_id = :o AND id = ANY(:ids)",
            o=owner,
            ids=question_ids,
        )
    } if question_ids else set()
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
         "trace_ids": [t for t in (gate.trace_id(r) for r in rows[:TRACE_IDS]) if t]}
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
         "trace_ids": [t for t in (gate.trace_id(key_rows.get(keys[q])) for q in ids[:TRACE_IDS]) if t]}
        for code, ids in by_code.items()
    ]
    tutor.sort(key=lambda g: (-int(g["count"]), str(g["error_code"])))
    return {"truncated": truncated, "jobs": jobs, "tutor": tutor}


async def _ingest(
    conn: AsyncConnection, owner: uuid.UUID, since: datetime, limit: int, gate: TraceGate
) -> dict[str, Any]:
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
            "trace_id": gate.trace_id(r),
        })
    return {"truncated": truncated, "rows": rows[:INGEST_LIMIT]}


async def trace_gate(conn: AsyncConnection, since: datetime, now: datetime, tracing: Any) -> TraceGate:
    """`tracing`: the api's settings (`enabled`, `retention_days`), FR-OBS-1."""
    periods = tuple((r.started_at, bool(r.enabled)) for r in await _rows(conn, PERIODS_SQL, s=since))
    oldest = now - timedelta(days=int(tracing.retention_days))
    return TraceGate(bool(tracing.enabled), periods, oldest)


# FR-OBS-17: ONE grouped query over the owner's spend rows of the window, by (lesson, run, tutor or not); a
# group's amount is FR-AGENT-9's spent: settled_usd of settles plus usd of reserves with no settle or void.
LESSON_SPEND_SQL: Final = (
    "SELECT s.lesson_id, s.run_id, s.task = :tutor AS tutor,"
    " sum(CASE WHEN s.kind = 'settle' THEN s.settled_usd"
    "  WHEN s.kind = 'reserve' AND NOT EXISTS (SELECT 1 FROM agent_spend x WHERE x.reservation_id = s.id)"
    "  THEN s.usd ELSE 0 END) AS amount,"
    " count(*) FILTER (WHERE s.kind = 'reserve'"
    "  AND NOT EXISTS (SELECT 1 FROM agent_spend x WHERE x.reservation_id = s.id)) AS open_reserves,"
    " max(s.created_at) AS newest"
    " FROM agent_spend s WHERE s.owner_id = :o AND s.created_at >= :s AND s.lesson_id IS NOT NULL"
    " GROUP BY s.lesson_id, s.run_id, s.task = :tutor"
)
LESSON_TITLES_SQL: Final = (
    "SELECT l.id, l.title, c.id AS course_id, c.title AS course_title FROM lessons l"
    " JOIN courses c ON c.id = l.course_id WHERE l.owner_id = :o AND l.id = ANY(:ids)"
    " AND l.deleted_at IS NULL AND c.deleted_at IS NULL"  # a tombstoned lesson or course shows as deleted
)
# SRV-PIPELINE's DbSpanReader's rows per lesson; owner-scoped at every table; the database returns numbers
LESSON_CHARS_SQL: Final = (
    "SELECT p.lesson_id, sum(char_length(s.text)) AS chars FROM course_plans p"
    " JOIN materials m ON m.sha256 = p.material_sha256 AND m.owner_id = :o"
    " JOIN ingests i ON i.material_id = m.id"
    " JOIN material_spans s ON s.ingest_id = i.id AND s.owner_id = :o"
    " AND s.page BETWEEN p.pdf_first AND p.pdf_last"
    " WHERE p.owner_id = :o AND p.lesson_id = ANY(:ids) GROUP BY p.lesson_id"
)
LESSONS_LIMIT: Final = 200
MLFLOW_LESSON_URL: Final = "https://localhost:8443/#/experiments/0/traces?q={}"


def _per_1k(total: Decimal, chars: int | None) -> float | None:
    if not chars:
        return None
    return float(round(total / (Decimal(chars) / 1000), 6))


async def _lessons(conn: AsyncConnection, owner: uuid.UUID, since: datetime) -> dict[str, Any]:
    groups = await _rows(conn, LESSON_SPEND_SQL, o=owner, s=since, tutor=TUTOR_TASK)
    per: dict[uuid.UUID, dict[str, Any]] = {}
    for g in groups:
        row = per.setdefault(g.lesson_id, {"compile": Decimal(0), "tutor": Decimal(0), "runs": {}, "open": 0})
        amount = Decimal(g.amount or 0)
        row["open"] += int(g.open_reserves or 0)
        if g.tutor:
            row["tutor"] += amount
        else:
            row["compile"] += amount
            row["runs"][g.run_id] = (g.newest, amount)
    ids = sorted(per)
    titles = {r.id: r for r in await _rows(conn, LESSON_TITLES_SQL, o=owner, ids=ids)} if ids else {}
    chars: dict[uuid.UUID, int] = {}
    if titles and (await conn.execute(text("SELECT to_regclass('material_spans')"))).scalar() is not None:
        found = await _rows(conn, LESSON_CHARS_SQL, o=owner, ids=list(titles))
        chars = {r.lesson_id: int(r.chars) for r in found}
    rows: list[dict[str, Any]] = []
    for lesson_id, p in per.items():
        t = titles.get(lesson_id)  # None: the lesson was deleted; its spend stays (totals equal the ledger)
        total = p["compile"] + p["tutor"]
        newest = max(p["runs"].values(), key=lambda v: v[0], default=None)
        rows.append({
            "lesson_id": str(lesson_id), "title": t.title if t else None,
            "course_id": str(t.course_id) if t else None, "course_title": t.course_title if t else None,
            "deleted": t is None, "compile_usd": _usd(p["compile"]), "runs": len(p["runs"]),
            "last_compile_usd": _usd(newest[1]) if newest else None, "tutor_usd": _usd(p["tutor"]),
            "total_usd": _usd(total), "open_reserves": p["open"], "chars": chars.get(lesson_id),
            "usd_per_1k_chars": _per_1k(total, chars.get(lesson_id)),
            "mlflow_url": MLFLOW_LESSON_URL.format(lesson_id),
        })
    rows.sort(key=lambda r: (-r["total_usd"], r["lesson_id"]))
    kept = rows[:LESSONS_LIMIT]
    courses: dict[str, dict[str, Any]] = {}
    for r in kept:
        if r["course_id"] is None:  # a deleted lesson joins no course total
            continue
        c = courses.setdefault(r["course_id"], {"title": r["course_title"], "compile": Decimal(0),
                                                "tutor": Decimal(0), "chars": 0})
        c["compile"] += Decimal(str(r["compile_usd"]))
        c["tutor"] += Decimal(str(r["tutor_usd"]))
        c["chars"] += r["chars"] or 0
    totals = []
    for course_id, c in sorted(courses.items()):
        whole = c["compile"] + c["tutor"]
        totals.append({"course_id": course_id, "title": c["title"], "compile_usd": _usd(c["compile"]),
                       "tutor_usd": _usd(c["tutor"]), "total_usd": _usd(whole), "chars": c["chars"] or None,
                       "usd_per_1k_chars": _per_1k(whole, c["chars"])})
    return {"truncated": len(rows) > LESSONS_LIMIT, "open_reserves": sum(r["open_reserves"] for r in kept),
            "rows": kept, "courses": totals}


async def summary(
    conn: AsyncConnection,
    owner: uuid.UUID,
    days: int,
    now: datetime,
    read_limit: int = READ_LIMIT,
    tracing: Any = None,
) -> dict[str, Any]:
    from reader.obs.setup import effective_settings

    since = now - timedelta(days=days)
    catalog = _kinds()
    # The api's settings with the retention override read per request (SRV-CONFIG FR-CONFIG-26, paired).
    tracing = tracing if tracing is not None else await effective_settings(conn)
    gate = await trace_gate(conn, since, now, tracing)
    return {
        "compile_runs": await _compile_runs(conn, owner, since, catalog, gate),
        "tutor": await _tutor(conn, owner, since, catalog, gate),
        "failures": await _failures(conn, owner, since, catalog, read_limit, gate),
        "ingest": await _ingest(conn, owner, since, read_limit, gate),
        "lessons": await _lessons(conn, owner, since),
    }
