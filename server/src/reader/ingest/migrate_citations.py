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
NO_TARGETS_EXIT = 2
TARGETS_MODULE = "reader.tutor.citations"


@dataclass(frozen=True)
class Target:
    """A citation-bearing column declared by its owning spec: the select returns (key, owner_id, document)
    rows locked FOR UPDATE; citations are any objects in the document with a `span` key (the VEC-TUTOR answer
    holds them in `blocks[*].citations[*]`); the update takes :key and :doc."""

    name: str
    select_sql: str
    update_sql: str


def targets() -> list[Target]:
    """The declared targets: SRV-TUTOR's `reader.tutor.citations.REWRITERS` (paired). Only that module being
    absent means none is declared; any other import error, or no REWRITERS, fails."""
    try:
        module = importlib.import_module(TARGETS_MODULE)
    except ModuleNotFoundError as error:
        if error.name != TARGETS_MODULE:
            raise
        return []
    return list(module.REWRITERS)


async def _walk(conn: Any, owner: Any, value: Any) -> tuple[Any, bool]:
    """The value with every mapped legacy `span` rewritten, anywhere in it; unmapped ones stay as they are."""
    if isinstance(value, list):
        items = [await _walk(conn, owner, v) for v in value]
        return [v for v, _ in items], any(c for _, c in items)
    if not isinstance(value, dict):
        return value, False
    out: dict[str, Any] = {}
    changed = False
    for name, item in value.items():
        out[name], sub = await _walk(conn, owner, item)
        changed = changed or sub
    span = out.get("span")
    if isinstance(span, str) and locator.is_legacy_id(span):
        found = await spanref.legacy_target(conn, owner, span)
        if found is not None:
            out, changed = {**out, "span": found[0], "quote_sha256": found[1]}, True
    return out, changed


def _legacy_left(value: Any) -> Counter[str]:
    """Legacy ids still under a `span` key, per material."""
    left: Counter[str] = Counter()
    if isinstance(value, list):
        for item in value:
            left.update(_legacy_left(item))
    elif isinstance(value, dict):
        for item in value.values():
            left.update(_legacy_left(item))
        span = value.get("span")
        if isinstance(span, str) and locator.is_legacy_id(span):
            left[span.split(":")[0]] += 1
    return left


async def _rewrite(conn: Any, target: Target, unmapped: Counter[str]) -> int:
    changed_rows = 0
    for key, owner, doc in (await conn.execute(text(target.select_sql))).all():
        out, changed = await _walk(conn, owner, doc)
        unmapped.update(_legacy_left(out))  # no legacy id may remain after the rewrite: each one is unmapped
        if changed:
            await conn.execute(text(target.update_sql), {"key": key, "doc": json.dumps(out)})
            changed_rows += 1
    return changed_rows


async def run(engine: AsyncEngine, declared: Sequence[Target]) -> tuple[int, dict[str, Any]]:
    if not declared:
        return NO_TARGETS_EXIT, {"result": "no_targets"}
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
