"""What the routes and the jobs share: the configured models and language, the sample with its scan call
texts, the estimate (FR-FRESH-2, -3, -9), the daily-cap check over the three tasks, and the report's read
shape (FR-FRESH-10, -11, -12).
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from reader.agent import ledger
from reader.agent.errors import AgentError
from reader.agent.pricing import resolve_model
from reader.config import ReaderConfig
from reader.freshness import params, prompts, scan, store
from reader.freshness import sample as sampling
from reader.freshness.bibliography import FIELDS
from reader.freshness.errors import FreshError
from reader.freshness.estimate import Estimate, estimate
from reader.freshness.sample import OutlineRow, Sample, Topic
from reader.freshness.signals import language

SAMPLE_CACHE_MAX: Final = 16
_SAMPLES: OrderedDict[tuple[uuid.UUID, str], Sample] = OrderedDict()
_CLAIM_KEYS: Final = ("id", "subject", "kind", "confidence", "page", "span")


def today_utc() -> date:
    return datetime.now(UTC).date()


def models(config: ReaderConfig) -> dict[str, str]:
    """The resolved model of each task; a missing task or an unknown model gives `config_unavailable`."""
    found: dict[str, str] = {}
    for task in params.TASKS:
        entry = config.tasks.get(task)
        if entry is None:
            raise FreshError("config_unavailable")
        try:
            found[task] = resolve_model(entry.model)
        except AgentError:
            raise FreshError("config_unavailable") from None
    return found


def language_of(config: ReaderConfig, material_sha256: str) -> str:
    for material in config.materials:
        if material.sha256 == material_sha256:
            return language(material.language)
    return "en"


@dataclass(frozen=True)
class Prepared:
    material: store.MaterialRow
    ingest_id: uuid.UUID
    sample: Sample
    outline: list[OutlineRow]
    language: str
    models: Mapping[str, str]
    scan_chars: int
    estimate: Estimate

    @property
    def scan_inputs_sha256(self) -> str:
        prompt = prompts.load("scan")
        ids = [sampling.span_id(self.material.sha256, seq) for seq in self.sample.seqs]
        model = self.models[params.TASK_SCAN]
        return sampling.scan_inputs_sha256(str(self.ingest_id), ids, prompt.version, prompt.sha256, model)

    @property
    def topics(self) -> list[Topic]:
        return sampling.topics(self.outline)


async def prepare(
    conn: AsyncConnection, config: ReaderConfig, owner_id: uuid.UUID, material: store.MaterialRow
) -> Prepared:
    if material.ingest_id is None:
        raise FreshError("not_imported")
    found = models(config)
    lang = language_of(config, material.sha256)
    outline = await store.outline(conn, owner_id, material.ingest_id)
    key = (material.ingest_id, lang)
    built = _SAMPLES.get(key)
    if built is None:
        spans = await store.spans(conn, owner_id, material.ingest_id)
        built = sampling.build(spans, outline, material.page_count, lang)
        _SAMPLES[key] = built
        while len(_SAMPLES) > SAMPLE_CACHE_MAX:
            _SAMPLES.popitem(last=False)
    system, user = scan.call_texts(built, prompts.load("scan"))
    chars = len(system) + len(user)
    est = estimate(found[params.TASK_SCAN], found[params.TASK_REPORT], chars)
    return Prepared(material, material.ingest_id, built, outline, lang, found, chars, est)


async def spent_today(engine: AsyncEngine, owner_id: uuid.UUID) -> Decimal:
    """The owner's spend today (UTC) over the three freshness tasks (FR-FRESH-9)."""
    total = Decimal(0)
    for task in params.TASKS:
        total += await ledger.spent(engine, owner_id=owner_id, task=task, day=True)
    return total


async def daily_refused(engine: AsyncEngine, owner_id: uuid.UUID, reserve: Decimal) -> bool:
    return await spent_today(engine, owner_id) + reserve > params.DAILY_CAP_USD


# ---- the read shape (FR-FRESH-10, -11) -----------------------------------------------------------------


def progress(report: store.Report) -> dict[str, Any] | None:
    if report.status != "running":
        return None
    if report["claims"] is None:
        return {"stage": "scan", "done": 0, "total": 1}
    searches = report["searches"]
    if not searches:
        if report["bibliography"] is None:  # a scan-only check about to finish
            return {"stage": "scan", "done": 1, "total": 1}
        return {"stage": "search", "done": 0, "total": 0}
    pending = sum(1 for s in searches if s.get("status") == "pending")
    if pending:
        return {"stage": "search", "done": len(searches) - pending, "total": len(searches)}
    return {"stage": "report", "done": 0, "total": 1}


def report_view(report: store.Report, topics: list[Topic] | None) -> dict[str, Any]:
    finished = report["finished_at"]
    claims = report["claims"]
    shown_topics = None
    if topics is not None and report["verdict"] is not None:
        shown_topics = [{"id": t.id, "title": t.title} for t in topics]
    return {
        "id": str(report.id),
        "status": report.status,
        "created_at": report["created_at"].isoformat(),
        "finished_at": finished.isoformat() if finished is not None else None,
        "error": report["error"],
        "progress": progress(report),
        "verdict": report["verdict"],
        "reasons": report["reasons"],
        "claims": None if claims is None else [_claim_view(c) for c in claims],
        "claim_status": report["claim_status"],
        "recommendations": report["recommendations"],
        "topics": shown_topics,
        "dropped": report["dropped"],
    }


def _claim_view(claim: Mapping[str, Any]) -> dict[str, Any]:
    """A stored claim in the read shape; `cell` stays null until a lesson cell can be linked."""
    return {**{k: claim[k] for k in _CLAIM_KEYS}, "cell": None}


def stale_reason(
    report: store.Report | None,
    current_ingest: uuid.UUID | None,
    bibliography_sha256: str | None,
    now: datetime,
) -> str | None:
    """FR-FRESH-10: only a `done` report is stale; reingested, then bibliography_changed, then ttl."""
    if report is None or report.status != "done":
        return None
    if report["ingest_id"] != current_ingest:
        return "reingested"
    used = report["bibliography"]
    if isinstance(used, dict) and used.get("sha256") is not None and used["sha256"] != bibliography_sha256:
        return "bibliography_changed"
    if now - report["created_at"] > timedelta(days=params.REPORT_TTL_DAYS):
        return "ttl"
    return None


def fields_of(bibliography: Mapping[str, Any]) -> dict[str, Any]:
    return {name: bibliography.get(name) for name in FIELDS}
