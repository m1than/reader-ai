"""SRV-LESSON property test through the write path, against Postgres.

@trace INV-LESSON-1 INV-LESSON-3 FR-LESSON-4
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from .conftest import F01, Pg, need, spec_data, world

KEYS = ["kind", "sectors", "provenance", "claims", "asserts", "front", "x"]


def _values() -> st.SearchStrategy[Any]:
    leaf = st.one_of(st.none(), st.booleans(), st.integers(-3, 3), st.text(max_size=8))
    return st.recursive(leaf, lambda inner: st.lists(inner, max_size=3), max_leaves=5)


_edits = st.lists(
    st.tuples(st.integers(0, 9), st.sampled_from(KEYS), _values()) | st.just(("drop-notes", "", None)),
    max_size=3,
)


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(sequence=st.lists(_edits, min_size=1, max_size=4))
def test_every_write_either_refuses_cleanly_or_stores_a_valid_revision(
    pg: Pg, tmp_path_factory: Any, sequence: list[list[tuple[Any, str, Any]]]
) -> None:
    """For any sequence of (possibly broken) notebooks written to one lesson: a refusal leaves no row and no
    stage record; an accepted write stores bytes that re-validate and hash to their name; and the notes cell
    ids along the stage records never shrink (INV-LESSON-3)."""
    schema = need("reader.lessons.schema")
    store = need("reader.lessons.store")
    codes = set(spec_data()["write_errors"])
    tmp = Path(tmp_path_factory.mktemp("prop"))

    async def run() -> None:
        from sqlalchemy import text

        w = await world(pg, tmp)
        async with w.engine.begin() as conn:
            course = await store.create_course(conn, w.users["ivan"], f"Property {tmp.name}", None)
            lesson = await store.ensure_lesson(conn, w.users["ivan"], course, 1, "P")
        for edits in sequence:
            doc = json.loads(F01)
            # Field edits first (by cell index), then the structural edit that removes a cell.
            for cell, key, value in sorted(edits, key=lambda e: e[0] == "drop-notes"):
                if cell == "drop-notes":
                    doc["cells"] = [c for c in doc["cells"] if c["id"] != "f01-notes"]
                else:
                    doc["cells"][cell]["metadata"]["reader"][key] = value
            async with w.engine.connect() as conn:
                before = (await conn.execute(text("SELECT count(*) FROM lesson_revision_stages"))).scalar()
            code: str | None = None
            try:
                async with w.engine.begin() as conn:
                    await store.write_revision(
                        conn, w.blobs, w.users["ivan"], lesson, json.dumps(doc).encode(), "draft", {}
                    )
            except store.LessonWriteError as error:
                code = str(error.code)
            if code is not None:
                assert code in codes
                async with w.engine.connect() as conn:
                    after = (await conn.execute(text("SELECT count(*) FROM lesson_revision_stages"))).scalar()
                assert after == before
        async with w.engine.connect() as conn:
            rows = (
                await conn.execute(
                    text("SELECT sha256 FROM lesson_revision_stages WHERE lesson_id = :l ORDER BY id"),
                    {"l": lesson},
                )
            ).scalars().all()
        previous: set[str] = set()
        for sha in rows:
            data = (w.blobs / "sha256" / sha[:2] / sha).read_bytes()
            assert hashlib.sha256(data).hexdigest() == sha
            notebook = schema.validate(data).notebook
            notes = schema.notes_ids(notebook)
            assert previous <= notes
            previous = notes
        await w.engine.dispose()

    asyncio.run(run())
