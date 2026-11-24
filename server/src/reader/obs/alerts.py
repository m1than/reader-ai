"""SRV-OBS FR-OBS-25 (ADR-0038): the alert codes on /observability, computed from Postgres rows only (the api
never reads MLflow). `compute` is pure; `load` reads the rows it needs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from reader.obs import health

SHARE: Final = 0.8  # of a cap
REGRESSION: Final = 0.05
STALE: Final = timedelta(seconds=health.STALE_AFTER_S)  # FR-OBS-32
BENCH_METRIC: Final = {"search": "hit_at_10", "tutor": "grounded_share"}


def compute(
    *,
    task_spend_today: Mapping[str, float],
    task_daily_caps: Mapping[str, float],
    open_runs: Sequence[tuple[str, float, float]],
    dead_jobs: Mapping[str, int],
    bench: Sequence[tuple[str, str, str | None, Mapping[str, float]]],
    stack: Mapping[str, tuple[bool, datetime]],
    now: datetime,
    tracing: bool = True,
) -> list[dict[str, Any]]:
    """`bench` is oldest first; `open_runs` is (run id, spend, cap); `stack` is member → (ok, checked_at).
    `task_spend_today` is keyed by cap subject (see `subject`). With tracing off nothing probes the stack, so
    its rows are stale by design and raise no `obs_unhealthy`."""
    out: list[dict[str, Any]] = []
    for task, spend in sorted(task_spend_today.items()):
        cap = task_daily_caps.get(task)
        if cap and spend >= SHARE * cap:
            out.append({"code": "daily_spend_high", "subject": task, "value": spend, "limit": SHARE * cap})
    for run, spend, cap in open_runs:
        if cap and spend >= SHARE * cap:
            out.append({"code": "run_over_cap", "subject": run, "value": spend, "limit": SHARE * cap})
    for kind, count in sorted(dead_jobs.items()):
        if count > 0:
            out.append({"code": "dead_jobs", "subject": kind, "value": count, "limit": 0})
    series: dict[tuple[str, str, str | None], list[Mapping[str, float]]] = {}
    for kind, set_name, model, scores in bench:
        series.setdefault((kind, set_name, model), []).append(scores)
    for (kind, set_name, model), rows in series.items():
        metric = BENCH_METRIC.get(kind)
        if metric is None or len(rows) < 2 or metric not in rows[-1] or metric not in rows[-2]:
            continue
        newest, previous = float(rows[-1][metric]), float(rows[-2][metric])
        if newest < previous - REGRESSION:
            subject = "/".join(p for p in (kind, set_name, model) if p)
            out.append(
                {
                    "code": "bench_regressed",
                    "subject": subject,
                    "value": newest,
                    "limit": round(previous - REGRESSION, 6),
                }
            )
    for member, (ok, checked_at) in sorted(stack.items() if tracing else ()):
        age = now - checked_at
        if not ok or health.stale(checked_at, now, tracing):  # FR-OBS-32's one staleness rule
            out.append(
                {
                    "code": "obs_unhealthy",
                    "subject": member,
                    "value": int(age.total_seconds() // 60),
                    "limit": int(STALE.total_seconds() // 60),
                }
            )
    return out


SPEND_TODAY_SQL: Final = (
    "SELECT task, sum(CASE WHEN kind = 'settle' THEN settled_usd WHEN kind = 'reserve' AND NOT EXISTS"
    " (SELECT 1 FROM agent_spend x WHERE x.reservation_id = s.id) THEN usd ELSE 0 END)"
    " FROM agent_spend s WHERE owner_id = :o AND created_at >= :day GROUP BY task"
)
OPEN_RUNS_SQL: Final = (
    "SELECT r.id::text, r.cap_usd, coalesce((SELECT sum(CASE WHEN s.kind = 'settle' THEN s.settled_usd"
    " WHEN s.kind = 'reserve' AND NOT EXISTS (SELECT 1 FROM agent_spend x WHERE x.reservation_id = s.id)"
    " THEN s.usd ELSE 0 END) FROM agent_spend s WHERE s.run_id = r.id), 0)"
    " FROM compile_runs r WHERE r.owner_id = :o AND r.finished_at IS NULL"
)
BENCH_SQL: Final = (
    "SELECT kind, set_name, model, scores FROM obs_bench_results WHERE owner_id = :o ORDER BY created_at, id"
)
STACK_SQL: Final = "SELECT member, ok, detail, checked_at FROM obs_stack_status ORDER BY member"


UNDEFINED_TABLE: Final = "42P01"


async def _rows(conn: Any, sql: str, params: Mapping[str, Any]) -> list[Any]:
    """A missing table (a module not migrated in this deployment) reads as no rows, in a savepoint; any other
    database error propagates (code review round 1 of phase 2: an error never reads as "no alert")."""
    try:
        async with conn.begin_nested():
            return list((await conn.execute(text(sql), params)).all())
    except ProgrammingError as failed:
        if _sqlstate(failed) != UNDEFINED_TABLE:
            raise
        return []


def _sqlstate(failed: ProgrammingError) -> str | None:
    orig = failed.orig
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    cause = getattr(orig, "__cause__", None)
    return str(code or getattr(cause, "sqlstate", None) or "") or None


# FR-FRESH-3's task names and FR-FRESH-9's daily cap, mirrored: INV-FRESH-5 quarantines `reader.freshness` (no
# module outside it imports it); a [unit] test keeps these equal to reader.freshness.params.
FRESHNESS_TASKS: Final = ("freshness_scan", "freshness_web", "freshness_report")
FRESHNESS_DAILY_CAP_USD: Final = Decimal("2.00")


def subject(task: str) -> str:
    """The daily cap a task's spend counts against: the freshness tasks share FR-FRESH-9's one daily cap."""
    return "freshness" if task in FRESHNESS_TASKS else task


def daily_caps() -> dict[str, float]:
    """FR-AGENT-12's per-task daily caps, by subject: the tutor's and the freshness check's."""
    from reader.tutor import params as tutor

    return {tutor.TASK: float(tutor.DAILY_CAP_USD), "freshness": float(FRESHNESS_DAILY_CAP_USD)}


async def stack(conn: Any) -> dict[str, dict[str, Any]]:
    return {
        str(m): {"ok": bool(ok), "detail": str(d), "checked_at": at.astimezone(UTC).isoformat()}
        for m, ok, d, at in await _rows(conn, STACK_SQL, {})
    }


async def load(
    conn: Any, owner: Any, dead_jobs: Mapping[str, int], now: datetime, *, tracing: bool = True
) -> list[dict[str, Any]]:
    day = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    spend: dict[str, float] = {}
    for t, v in await _rows(conn, SPEND_TODAY_SQL, {"o": owner, "day": day}):
        spend[subject(str(t))] = spend.get(subject(str(t)), 0.0) + float(Decimal(v or 0))
    caps = daily_caps()
    runs = [
        (str(i), float(Decimal(s or 0)), float(c))
        for i, c, s in await _rows(conn, OPEN_RUNS_SQL, {"o": owner})
    ]
    bench = [
        (str(k), str(s), m, dict(sc or {})) for k, s, m, sc in await _rows(conn, BENCH_SQL, {"o": owner})
    ]
    members = {str(m): (bool(ok), at) for m, ok, _, at in await _rows(conn, STACK_SQL, {})}
    return compute(
        task_spend_today=spend,
        task_daily_caps=caps,
        open_runs=runs,
        dead_jobs=dead_jobs,
        bench=bench,
        stack=members,
        now=now,
        tracing=tracing,
    )
