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
    """The material's active pack (SRV-INGEST INV-INGEST-16, `reader.ingest.views`); no pack tables yet (its
    migration not merged) or no active pack reads as no spans."""

    async def load(
        self, conn: AsyncConnection, owner_id: uuid.UUID, material_sha256: str, first: int, last: int
    ) -> tuple[list[Span], str]:
        present = (await conn.execute(text("SELECT to_regclass('base_packs')"))).scalar()
        if present is None:
            return [], material_sha256[:12]
        from reader.ingest import views

        pack = await views.active_pack(conn, owner_id, material_sha256)
        if pack is None:
            return [], material_sha256[:12]
        spans = await views.pack_spans(conn, pack, first, last)
        config_id = (
            await conn.execute(
                text("SELECT config_id FROM materials WHERE owner_id = :o AND sha256 = :m"),
                {"o": owner_id, "m": material_sha256},
            )
        ).scalar()
        title = str(config_id) if spans and config_id else material_sha256[:12]
        return [Span(s.span_id, s.page, s.kind, s.text) for s in spans], title


async def material_imported(conn: AsyncConnection, owner_id: uuid.UUID, material_sha256: str) -> bool:
    if (await conn.execute(text("SELECT to_regclass('base_packs')"))).scalar() is None:
        return False
    from reader.ingest import views

    # FR-PIPELINE-22: a deleted material (SRV-INGEST FR-INGEST-31) counts as not imported; "imported" is "has an
    # active pack" (FR-PIPELINE-2's paired change, INV-INGEST-16), and the tombstone is checked as well.
    live = await conn.execute(
        text("SELECT 1 FROM materials WHERE owner_id = :o AND sha256 = :m AND deleted_at IS NULL"),
        {"o": owner_id, "m": material_sha256},
    )
    return live.first() is not None and await views.active_pack(conn, owner_id, material_sha256) is not None
