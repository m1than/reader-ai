"""`python -I -m reader.ingest.migrate_citations`: rewrite mutable legacy citations (SRV-INGEST FR-INGEST-41).

One transaction over every declared target: each legacy id `<sha>:<seq>` becomes the accepted map's
locator plus its `quote_sha256`; any unmapped id rolls everything back and exits 4 `unmapped` with counts
per material only.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from reader.ingest import locator, spanref, store

UNMAPPED_EXIT = 4


@dataclass(frozen=True)
class Target:
    """A citation-bearing column declared by its owning spec: the select returns (key, owner_id, document)
    rows locked FOR UPDATE, the document's `citations` holds `{span, …}` objects, the update takes :key and
    :doc."""

    name: str
    select_sql: str
    update_sql: str


def targets() -> list[Target]:
    """The declared targets: SRV-TUTOR's `reader.tutor.citations.REWRITERS` (paired), empty until then."""
    try:
        module = importlib.import_module("reader.tutor.citations")
    except ModuleNotFoundError:
        return []
    return list(getattr(module, "REWRITERS", ()))


async def _rewrite(conn: Any, target: Target, unmapped: Counter[str]) -> int:
    changed_rows = 0
    for key, owner, doc in (await conn.execute(text(target.select_sql))).all():
        citations = doc.get("citations") if isinstance(doc, dict) else None
        if not isinstance(citations, list):
            continue
        out, changed = [], False
        for cite in citations:
            span = cite.get("span") if isinstance(cite, dict) else None
            if isinstance(span, str) and locator.is_legacy_id(span):
                found = await spanref.legacy_target(conn, owner, span)
                if found is None:
                    unmapped[span.split(":")[0]] += 1
                else:
                    cite, changed = {**cite, "span": found[0], "quote_sha256": found[1]}, True
            out.append(cite)
        if changed:
            body = json.dumps({**doc, "citations": out})
            await conn.execute(text(target.update_sql), {"key": key, "doc": body})
            changed_rows += 1
    return changed_rows


async def run(engine: AsyncEngine, declared: Sequence[Target]) -> tuple[int, dict[str, Any]]:
    async with engine.connect() as conn:
        await conn.begin()
        unmapped: Counter[str] = Counter()
        rows = {t.name: await _rewrite(conn, t, unmapped) for t in declared}
        if unmapped:
            await conn.rollback()
            return UNMAPPED_EXIT, {"result": "unmapped", "unmapped": dict(unmapped)}
        owner = await store.owner_id(conn)
        for name, count in rows.items():
            await conn.execute(text(
                "INSERT INTO citation_migrations (owner_id, target, rows) VALUES (:o, :t, :n)"),
                {"o": owner, "t": name, "n": count})
        await conn.commit()
    return 0, {"result": "migrated", "rows": rows}


def main(argv: list[str] | None = None) -> int:
    from reader.ingest.runtime import emit, runtime

    del argv

    async def work() -> tuple[int, dict[str, Any]]:
        async with runtime() as rt:
            return await run(rt.engine, targets())

    try:
        code, reply = asyncio.run(work())
    except Exception:  # driver or IO detail is never printed
        emit({"error": "unavailable"})
        return 1
    emit(reply)
    return code


if __name__ == "__main__":
    sys.exit(main())
