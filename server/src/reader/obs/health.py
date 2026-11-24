"""SRV-OBS FR-OBS-32 (ADR-0038): the stack health probe. A periodic `*/30` entry enqueues one `obs.health`
job per 30-minute slot; the job probes MLflow's /health and Rotel's OTLP HTTP listener and writes the newest
result per member to `obs_stack_status`. The kind is untraced (`traced: false`), so probing adds no trace of
its own. Any HTTP answer from Rotel means it is listening (its OTLP receiver refuses a bare GET with 4xx)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Final

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from reader.jobs.model import JobKind
from reader.obs.phase2 import MLFLOW

log = logging.getLogger("reader.obs")
KIND: Final = "obs.health"
CRON: Final = "*/30 * * * *"
ROTEL: Final = "http://rotel:4318/v1/traces"
PROBE_TIMEOUT_S: Final = 5.0
STALE_AFTER_S: Final = 75 * 60  # FR-OBS-32: two missed 30-minute probes and a margin
STORE_BYTES_MAX: Final = 4 * 1024**3  # FR-OBS-31: structural; doctor/status warn at the ratio below
STORE_WARN_RATIO: Final = 0.8


class HealthArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slot: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:(00|30):00Z$")


def slot(at: datetime) -> str:
    at = at.astimezone(UTC)
    return at.replace(minute=at.minute - at.minute % 30, second=0, microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:00Z"
    )


async def probe(client: httpx.AsyncClient) -> dict[str, tuple[bool, str]]:
    out: dict[str, tuple[bool, str]] = {}
    try:
        r = await client.get(MLFLOW + "/health", timeout=PROBE_TIMEOUT_S)
        out["mlflow"] = (True, "healthy") if r.status_code == 200 else (False, "unhealthy")
    except httpx.HTTPError:
        out["mlflow"] = (False, "unreachable")
    try:
        await client.get(ROTEL, timeout=PROBE_TIMEOUT_S)
        out["rotel"] = (True, "listening")
    except httpx.HTTPError:
        out["rotel"] = (False, "unreachable")
    return out


UPSERT_SQL: Final = (
    "INSERT INTO obs_stack_status (member, ok, detail, checked_at) VALUES (:m, :ok, :d, :at)"
    " ON CONFLICT (member) DO UPDATE SET ok = EXCLUDED.ok, detail = EXCLUDED.detail,"
    " checked_at = EXCLUDED.checked_at"
)


async def handle(args: HealthArgs, conn: Any, ctx: Any) -> None:
    from reader.obs import setup as obs_setup

    if not obs_setup.current_settings().enabled:
        return
    async with httpx.AsyncClient(trust_env=False) as client:
        got = await probe(client)
    now = datetime.now(UTC)
    for member, (ok, detail) in got.items():
        await conn.execute(text(UPSERT_SQL), {"m": member, "ok": ok, "d": detail, "at": now})


async def enqueue_slot(jobs: Any, at: datetime) -> None:
    await jobs.enqueue(KIND, {"slot": slot(at)})


KIND_DECL: Final = JobKind(
    name=KIND,
    queue="obs",
    args=HealthArgs,
    key=("slot",),
    handler=handle,
    max_attempts=1,
    transient=(),
    timeout_s=20,
    traced=False,
    periodic=(CRON, lambda at: {"slot": slot(at)}),
)
