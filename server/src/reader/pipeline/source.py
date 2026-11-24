"""The lesson's source: spans by page range, aliases and delimited rendering (FR-PIPELINE-2, VEC-PIPELINE-5).

Span k (1-based, in `seq` order) gets the alias `s<k>`; the model sees and cites only aliases, and code
expands them to SRV-INGEST span ids. Each span renders as a marker line
`⟦source⟧ s<k> p.<page> <kind> ⟦/source⟧` and its text, in which the marker characters are replaced
(⟦ → [, ⟧ → ]), so span text cannot forge a marker.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from reader.pipeline.errors import PipelineError

SOURCE_MAX_CHARS: Final = 300000  # bounds.source_max_chars
OPEN: Final = "⟦source⟧"
CLOSE: Final = "⟦/source⟧"


@dataclass(frozen=True)
class Span:
    span_id: str
    page: int
    kind: str
    text: str


@dataclass(frozen=True)
class Source:
    text: str
    sha256: str
    aliases: Mapping[str, Span]


def _escape(value: str) -> str:
    return value.replace("⟦", "[").replace("⟧", "]")


def render(spans: Sequence[Span]) -> Source:
    if not spans:
        raise PipelineError("no_source_spans")
    if sum(len(s.text) for s in spans) > SOURCE_MAX_CHARS:
        raise PipelineError("source_too_large")
    aliases: dict[str, Span] = {}
    blocks: list[str] = []
    for k, span in enumerate(spans, start=1):
        alias = f"s{k}"
        aliases[alias] = span
        blocks.append(f"{OPEN} {alias} p.{span.page} {_escape(span.kind)} {CLOSE}\n{_escape(span.text)}")
    rendered = "\n\n".join(blocks)
    return Source(rendered, hashlib.sha256(rendered.encode("utf-8")).hexdigest(), aliases)


class SpanReader(Protocol):
    async def load(
        self, conn: AsyncConnection, owner_id: uuid.UUID, material_sha256: str, first: int, last: int
    ) -> tuple[list[Span], str]: ...


class DbSpanReader:
    """SRV-INGEST's tables (FR-INGEST-14); none yet (its migration not merged) reads as no spans."""

    async def load(
        self, conn: AsyncConnection, owner_id: uuid.UUID, material_sha256: str, first: int, last: int
    ) -> tuple[list[Span], str]:
        present = (await conn.execute(text("SELECT to_regclass('material_spans')"))).scalar()
        if present is None:
            return [], material_sha256[:12]
        rows = (
            await conn.execute(
                text(
                    "SELECT s.seq, s.page, s.kind, s.text, m.config_id FROM materials m"
                    " JOIN ingests i ON i.material_id = m.id JOIN material_spans s ON s.ingest_id = i.id"
                    " WHERE m.owner_id = :o AND m.sha256 = :m AND s.page BETWEEN :f AND :l ORDER BY s.seq"
                ),
                {"o": owner_id, "m": material_sha256, "f": first, "l": last},
            )
        ).all()
        title = str(rows[0][4]) if rows and rows[0][4] else material_sha256[:12]
        return [Span(f"{material_sha256}:{r[0]}", int(r[1]), str(r[2]), str(r[3])) for r in rows], title


async def material_imported(conn: AsyncConnection, owner_id: uuid.UUID, material_sha256: str) -> bool:
    if (await conn.execute(text("SELECT to_regclass('materials')"))).scalar() is None:
        return False
    found = await conn.execute(
        # FR-PIPELINE-22: a deleted material (SRV-INGEST FR-INGEST-31) is not imported for a new run or import.
        text("SELECT 1 FROM materials WHERE owner_id = :o AND sha256 = :m AND deleted_at IS NULL"),
        {"o": owner_id, "m": material_sha256},
    )
    return found.scalar() is not None
