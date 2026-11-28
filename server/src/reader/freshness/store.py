"""SQL for SRV-FRESH's tables and its reads of SRV-INGEST's active packs through `reader.ingest.views`
(FR-FRESH-1, -2, -10, -11; SRV-INGEST INV-INGEST-16).

Every query is scoped by owner. Report updates match only `running` rows, so a report is never changed after
`done`, `failed` or `budget_refused` (FR-FRESH-10). Nothing outside `reader.freshness` names these tables
(INV-FRESH-5).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from reader.freshness.bibliography import FIELDS, with_meta
from reader.freshness.errors import FreshError
from reader.freshness.sample import OutlineRow, SpanRow
from reader.ingest import views

JSON_COLUMNS: Final = (
    "bibliography",
    "sample",
    "claims",
    "scan_bibliography",
    "searches",
    "reasons",
    "claim_status",
    "recommendations",
    "dropped",
    "models",
    "prompt_versions",
)
COLUMNS: Final = (
    "id",
    "owner_id",
    "material_id",
    "ingest_id",
    "status",
    "scan_inputs_sha256",
    "inputs_sha256",
    "scan_reused_from",
    *JSON_COLUMNS,
    "verdict",
    "web_search_requests",
    "cost_usd",
    "error",
    "created_at",
    "finished_at",
)
FIXED: Final = frozenset({"id", "owner_id", "material_id", "ingest_id", "created_at"})
FINAL: Final = ("done", "failed", "budget_refused")


@dataclass(frozen=True)
class MaterialRow:
    id: uuid.UUID
    sha256: str
    config_id: str
    page_count: int
    ingest_id: uuid.UUID | None
    deleted: bool = False  # SRV-INGEST FR-INGEST-31's tombstone (FR-FRESH-20)


@dataclass(frozen=True)
class Report:
    data: Mapping[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    @property
    def id(self) -> uuid.UUID:
        value: uuid.UUID = self.data["id"]
        return value

    @property
    def status(self) -> str:
        return str(self.data["status"])


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


_MATERIAL: Final = (
    "SELECT m.id, m.sha256, m.config_id, m.page_count, m.deleted_at IS NOT NULL FROM materials m"
    " WHERE m.owner_id = :o AND "
)


async def _material(conn: AsyncConnection, owner_id: uuid.UUID, row: Any) -> MaterialRow | None:
    """The ingest is the one of the material's pack in the owner's head entry (FR-FRESH-2's paired change)."""
    if row is None:
        return None
    pack = await views.active_pack(conn, owner_id, str(row[1]))
    ingest_id = None if pack is None else pack.ingest_id
    return MaterialRow(row[0], str(row[1]), str(row[2]), int(row[3]), ingest_id, bool(row[4]))


async def live_material(conn: AsyncConnection, owner_id: uuid.UUID, ident: str) -> MaterialRow | None:
    """FR-FRESH-20: `find_material`, but nothing for a deleted material (the routes answer 404)."""
    found = await find_material(conn, owner_id, ident)
    return None if found is None or found.deleted else found


async def find_material(conn: AsyncConnection, owner_id: uuid.UUID, ident: str) -> MaterialRow | None:
    """The owner's material by its uuid or its reader.yaml id (`config_id`)."""
    as_uuid = _uuid(ident)
    if as_uuid is not None:
        found = await material(conn, owner_id, as_uuid)
        if found is not None:
            return found
    sql = _MATERIAL + "m.config_id = :c ORDER BY m.created_at, m.id LIMIT 1"
    row = (await conn.execute(text(sql), {"o": owner_id, "c": ident})).first()
    return await _material(conn, owner_id, row)


async def material(conn: AsyncConnection, owner_id: uuid.UUID, material_id: uuid.UUID) -> MaterialRow | None:
    row = (await conn.execute(text(_MATERIAL + "m.id = :m"), {"o": owner_id, "m": material_id})).first()
    return await _material(conn, owner_id, row)


async def _pack(conn: AsyncConnection, owner_id: uuid.UUID, ingest_id: uuid.UUID) -> views.Pack | None:
    pack = await views.pack_of_ingest(conn, ingest_id)
    return pack if pack is not None and pack.owner_id == owner_id else None


async def spans(conn: AsyncConnection, owner_id: uuid.UUID, ingest_id: uuid.UUID) -> list[SpanRow]:
    pack = await _pack(conn, owner_id, ingest_id)
    if pack is None:
        return []
    rows = await views.pack_spans(conn, pack)
    return [SpanRow(s.seq, s.page, s.kind, s.text, s.span_id, s.quote_sha256) for s in rows]


async def outline(conn: AsyncConnection, owner_id: uuid.UUID, ingest_id: uuid.UUID) -> list[OutlineRow]:
    pack = await _pack(conn, owner_id, ingest_id)
    if pack is None:
        return []
    return [OutlineRow(e.seq, e.level, e.title, e.page) for e in await views.pack_outline(conn, pack)]


async def handle_of(conn: AsyncConnection, owner_id: uuid.UUID) -> str:
    row = await conn.execute(text("SELECT handle FROM users WHERE id = :o"), {"o": owner_id})
    return str(row.scalar_one())


# ---- bibliography ----------------------------------------------------------------------------------------

_BIB_COLUMNS: Final = ", ".join(FIELDS)


async def bibliography(
    conn: AsyncConnection, owner_id: uuid.UUID, material_id: uuid.UUID
) -> dict[str, Any] | None:
    """The stored bibliography in its read shape (`...fields, source, sha256`), or None."""
    sql = (
        f"SELECT {_BIB_COLUMNS}, source FROM material_bibliography"  # noqa: S608 (fixed column names)
        " WHERE owner_id = :o AND material_id = :m"
    )
    row = (await conn.execute(text(sql), {"o": owner_id, "m": material_id})).first()
    if row is None:
        return None
    fields = {name: (list(row[i]) if isinstance(row[i], list) else row[i]) for i, name in enumerate(FIELDS)}
    return with_meta(fields, str(row[len(FIELDS)]))


async def save_bibliography(
    conn: AsyncConnection,
    owner_id: uuid.UUID,
    material_id: uuid.UUID,
    fields: Mapping[str, Any],
    source: str,
) -> bool:
    """Write the bibliography. `owner` replaces any entry; `scan` writes only where no owner entry exists."""
    shaped = with_meta(fields, source)
    params = {name: shaped[name] for name in FIELDS}
    params |= {"o": owner_id, "m": material_id, "source": source, "sha": shaped["sha256"]}
    values = ", ".join(f":{name}" for name in FIELDS)
    updates = ", ".join(f"{name} = EXCLUDED.{name}" for name in FIELDS)
    condition = "" if source == "owner" else " WHERE material_bibliography.source = 'scan'"
    sql = (
        f"INSERT INTO material_bibliography (owner_id, material_id, {_BIB_COLUMNS}, source, sha256)"  # noqa: S608
        f" VALUES (:o, :m, {values}, :source, :sha) ON CONFLICT (owner_id, material_id) DO UPDATE SET"
        f" {updates}, source = EXCLUDED.source, sha256 = EXCLUDED.sha256, updated_at = now(){condition}"
    )
    result = await conn.execute(text(sql), params)
    return bool(result.rowcount)


# ---- reports ---------------------------------------------------------------------------------------------

_SELECT: Final = f"SELECT {', '.join(COLUMNS)} FROM freshness_reports"  # noqa: S608 (fixed column names)


def _report(row: Any) -> Report | None:
    if row is None:
        return None
    data = dict(zip(COLUMNS, row, strict=True))
    for name in JSON_COLUMNS:
        data[name] = _json(data[name])
    return Report(data)


async def load(conn: AsyncConnection, report_id: uuid.UUID) -> Report | None:
    return _report((await conn.execute(text(_SELECT + " WHERE id = :r"), {"r": report_id})).first())


async def load_owned(
    conn: AsyncConnection, owner_id: uuid.UUID, material_id: uuid.UUID, report_id: uuid.UUID
) -> Report | None:
    sql = _SELECT + " WHERE id = :r AND owner_id = :o AND material_id = :m"
    row = (await conn.execute(text(sql), {"r": report_id, "o": owner_id, "m": material_id})).first()
    return _report(row)


async def latest(conn: AsyncConnection, owner_id: uuid.UUID, material_id: uuid.UUID) -> Report | None:
    sql = _SELECT + " WHERE owner_id = :o AND material_id = :m ORDER BY created_at DESC, id DESC LIMIT 1"
    return _report((await conn.execute(text(sql), {"o": owner_id, "m": material_id})).first())


async def latest_scan_bibliography(
    conn: AsyncConnection, owner_id: uuid.UUID, material_id: uuid.UUID
) -> dict[str, Any] | None:
    """The latest report's scan bibliography that has one, in its read shape (`...fields, sha256`)."""
    sql = (
        "SELECT scan_bibliography FROM freshness_reports WHERE owner_id = :o AND material_id = :m"
        " AND scan_bibliography IS NOT NULL ORDER BY created_at DESC, id DESC LIMIT 1"
    )
    fields = _json((await conn.execute(text(sql), {"o": owner_id, "m": material_id})).scalar())
    return with_meta(fields, None) if isinstance(fields, dict) else None


async def running_for(conn: AsyncConnection, owner_id: uuid.UUID, material_id: uuid.UUID) -> bool:
    """FR-FRESH-20: a `running` report of this material (SRV-INGEST FR-INGEST-31 refuses its delete)."""
    sql = "SELECT 1 FROM freshness_reports WHERE owner_id = :o AND material_id = :m AND status = 'running'"
    return (await conn.execute(text(sql), {"o": owner_id, "m": material_id})).first() is not None


async def running_of(conn: AsyncConnection, owner_id: uuid.UUID) -> list[Report]:
    rows = await conn.execute(text(_SELECT + " WHERE owner_id = :o AND status = 'running'"), {"o": owner_id})
    return [r for r in (_report(row) for row in rows) if r is not None]


def _dump(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


async def insert(
    engine: AsyncEngine,
    owner_id: uuid.UUID,
    material_id: uuid.UUID,
    ingest_id: uuid.UUID,
    scan_inputs_sha256: str,
    bibliography_used: Mapping[str, Any] | None,
    sample: Sequence[Mapping[str, Any]],
) -> uuid.UUID:
    """A `running` report; a second running check of the same user gives `freshness_running`."""
    sql = (
        "INSERT INTO freshness_reports (owner_id, material_id, ingest_id, status, scan_inputs_sha256,"
        " bibliography, sample) VALUES (:o, :m, :i, 'running', :s, CAST(:b AS jsonb),"
        " CAST(:sample AS jsonb)) RETURNING id"
    )
    values = {
        "o": owner_id,
        "m": material_id,
        "i": ingest_id,
        "s": scan_inputs_sha256,
        "b": _dump(None if bibliography_used is None else dict(bibliography_used)),
        "sample": _dump(list(sample)),
    }
    try:
        async with engine.begin() as conn:
            report_id: uuid.UUID = (await conn.execute(text(sql), values)).scalar_one()
    except IntegrityError:
        raise FreshError("freshness_running") from None
    return report_id


def _assignments(fields: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    parts: list[str] = []
    values: dict[str, Any] = {}
    for name, value in fields.items():
        if name not in COLUMNS or name in FIXED:
            raise ValueError("not an updatable column")
        key = f"v_{name}"
        if name in JSON_COLUMNS:
            parts.append(f"{name} = CAST(:{key} AS jsonb)")
            values[key] = _dump(value)
        elif name == "cost_usd":
            parts.append(f"{name} = CAST(:{key} AS numeric)")
            values[key] = None if value is None else Decimal(value)
        else:
            parts.append(f"{name} = :{key}")
            values[key] = value
    return ", ".join(parts), values


async def update(engine: AsyncEngine, report_id: uuid.UUID, **fields: Any) -> bool:
    """Update a `running` report in its own transaction (the routes see it at once); False when final."""
    assignments, values = _assignments(fields)
    sql = f"UPDATE freshness_reports SET {assignments} WHERE id = :r AND status = 'running'"  # noqa: S608
    async with engine.begin() as conn:
        result = await conn.execute(text(sql), {**values, "r": report_id})
    return bool(result.rowcount)


async def finish(engine: AsyncEngine, report_id: uuid.UUID, status: str, **fields: Any) -> bool:
    """The report's last write: a final status, `finished_at`, and the given fields."""
    if status not in FINAL:
        raise ValueError("not a final status")
    assignments, values = _assignments({"status": status, **fields})
    sql = (
        f"UPDATE freshness_reports SET {assignments}, finished_at = now()"  # noqa: S608
        " WHERE id = :r AND status = 'running'"
    )
    async with engine.begin() as conn:
        result = await conn.execute(text(sql), {**values, "r": report_id})
    return bool(result.rowcount)


async def reusable_scan(
    conn: AsyncConnection,
    owner_id: uuid.UUID,
    material_id: uuid.UUID,
    scan_inputs_sha256: str,
    exclude: uuid.UUID,
) -> Report | None:
    """FR-FRESH-3: the latest other report of the material with the same scan inputs and stored claims."""
    sql = (
        _SELECT + " WHERE owner_id = :o AND material_id = :m AND scan_inputs_sha256 = :s"
        " AND claims IS NOT NULL AND id <> :x ORDER BY created_at DESC, id DESC LIMIT 1"
    )
    values = {"o": owner_id, "m": material_id, "s": scan_inputs_sha256, "x": exclude}
    return _report((await conn.execute(text(sql), values)).first())


async def settled_of(engine: AsyncEngine, run_id: uuid.UUID, stage: str, attempt: int) -> Decimal | None:
    """One call's settled amount (SRV-AGENT ledger rows by run, stage and attempt): $0 when nothing was
    reserved; None for a reservation without a settlement, which counts as paid (FR-FRESH-12)."""
    sql = (
        "SELECT r.id, (SELECT s.settled_usd FROM agent_spend s WHERE s.reservation_id = r.id"
        " AND s.kind = 'settle') FROM agent_spend r WHERE r.run_id = :r AND r.stage = :s"
        " AND r.attempt = :a AND r.kind = 'reserve' ORDER BY r.id DESC LIMIT 1"
    )
    async with engine.connect() as conn:
        row = (await conn.execute(text(sql), {"r": run_id, "s": stage, "a": attempt})).first()
    if row is None:
        return Decimal(0)
    return None if row[1] is None else Decimal(row[1])


def age_s(report: Report, now: datetime) -> float:
    created: datetime = report["created_at"]
    return (now - created).total_seconds()
