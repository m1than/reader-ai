"""SRV-LESSON write path, internal functions and schema constraints, against Postgres.

@trace FR-LESSON-4 FR-LESSON-5 FR-LESSON-6 FR-LESSON-14 INV-LESSON-1 INV-LESSON-2 INV-LESSON-3
@trace INV-LESSON-6 INV-LESSON-7 VEC-LESSON-3 FR-LESSON-32
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from .conftest import (
    F01,
    Pg,
    case_bytes,
    cases,
    course_and_lesson,
    need,
    refusal,
    scalar,
    world,
    write,
)

pytestmark = pytest.mark.anyio
F01_SHA = hashlib.sha256(F01).hexdigest()
TABLES = (
    "SELECT count(*) FROM pg_tables"
    " WHERE tablename IN ('courses', 'lessons', 'lesson_revisions', 'lesson_revision_stages')"
)


async def _counts(w: Any) -> tuple[int, int]:
    return (
        await scalar(w.engine, "SELECT count(*) FROM lesson_revisions"),
        await scalar(w.engine, "SELECT count(*) FROM lesson_revision_stages"),
    )


async def test_write_creates_blob_revision_and_stage(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    _, lesson = await course_and_lesson(w)
    sha, created = await write(w, "ivan", lesson, stage="draft", provenance={"model": "m", "cost_usd": 0.5})
    assert (sha, created) == (F01_SHA, True)
    assert (w.blobs / "sha256" / sha[:2] / sha).read_bytes() == F01
    row = await scalar(
        w.engine,
        "SELECT json_build_object('size', byte_size, 'cells', cell_count, 'owner', owner_id::text)"
        " FROM lesson_revisions WHERE lesson_id=:l",
        l=lesson,
    )
    assert row == {"size": len(F01), "cells": 10, "owner": str(w.users["ivan"])}
    stage = await scalar(
        w.engine, "SELECT json_build_object('s', stage, 'p', provenance) FROM lesson_revision_stages"
    )
    assert stage == {"s": "draft", "p": {"model": "m", "cost_usd": 0.5}}
    assert await scalar(w.engine, "SELECT updated_at > created_at FROM lessons WHERE id=:l", l=lesson)
    await w.engine.dispose()


async def test_same_notebook_twice_is_one_revision_two_stage_records(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    _, lesson = await course_and_lesson(w)
    assert (await write(w, "ivan", lesson, stage="draft"))[1] is True
    wide = json.dumps(json.loads(F01), indent=3).encode()
    assert await write(w, "ivan", lesson, wide, stage="fact_check") == (F01_SHA, False)
    assert await _counts(w) == (1, 2)
    stages = await scalar(w.engine, "SELECT array_agg(stage ORDER BY id) FROM lesson_revision_stages")
    assert stages == ["draft", "fact_check"]
    await w.engine.dispose()


async def test_missing_and_foreign_lessons_are_one_code(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    _, lesson = await course_and_lesson(w, "ivan")
    assert await refusal(write(w, "anna", lesson)) == "unknown_lesson"
    assert await refusal(write(w, "ivan", uuid.uuid4())) == "unknown_lesson"
    assert await _counts(w) == (0, 0)
    await w.engine.dispose()


@pytest.mark.parametrize("case", [c for c in cases()["cases"] if c["write"]], ids=lambda c: str(c["id"]))
async def test_refused_writes_leave_nothing(pg: Pg, tmp_path: Path, case: dict[str, Any]) -> None:
    w = await world(pg, tmp_path)
    _, lesson = await course_and_lesson(w)
    assert await refusal(write(w, "ivan", lesson, case_bytes(case))) == case["write"]
    assert await _counts(w) == (0, 0)
    assert not any(p.is_file() for p in w.blobs.rglob("*"))
    await w.engine.dispose()


@pytest.mark.parametrize("case", cases()["successor_cases"], ids=lambda c: str(c["id"]))
async def test_successor_cases(pg: Pg, tmp_path: Path, case: dict[str, Any]) -> None:
    w = await world(pg, tmp_path)
    _, lesson = await course_and_lesson(w)
    await write(w, "ivan", lesson)
    if case["write"] is None:
        assert (await write(w, "ivan", lesson, case_bytes(case)))[1] is True
    else:
        assert await refusal(write(w, "ivan", lesson, case_bytes(case))) == case["write"]
        assert await _counts(w) == (1, 1)
    await w.engine.dispose()


@pytest.mark.parametrize(
    ("stage", "provenance"),
    [
        ("publish", {}),
        ("draft", {"secret_sauce": "x"}),
        ("draft", {"model": {"nested": 1}}),
        ("draft", {"model": [1]}),
        ("draft", {"source": "x" * 16400}),
        ("draft", {"source": "\ud800"}),  # lone surrogate: not encodable as UTF-8
        ("draft", {"cost_usd": 10**5000}),  # past Python's int-to-str limit
        ("draft", {"source": "a\x00b"}),  # Postgres jsonb cannot hold U+0000
    ],
    ids=["stage", "key", "nested", "list", "size", "surrogate", "huge-int", "nul"],
)
async def test_bad_provenance(pg: Pg, tmp_path: Path, stage: str, provenance: dict[str, Any]) -> None:
    w = await world(pg, tmp_path)
    _, lesson = await course_and_lesson(w)
    assert await refusal(write(w, "ivan", lesson, stage=stage, provenance=provenance)) == "bad_provenance"
    assert await _counts(w) == (0, 0)
    assert not any(p.is_file() for p in w.blobs.rglob("*")), "refused before any blob is written"
    await w.engine.dispose()


def _variant(n: int, extra_notes: str | None = None) -> bytes:
    doc = json.loads(F01)
    doc["cells"][0]["source"] = [f"variant {n}"]
    if extra_notes:
        extra = json.loads(json.dumps(doc["cells"][8]))
        extra["id"] = extra_notes
        doc["cells"].append(extra)
    return json.dumps(doc).encode()


async def test_notes_check_uses_the_current_revision(pg: Pg, tmp_path: Path) -> None:
    """A, then B (adds n2), then A again: A is an existing revision, but it drops B's n2."""
    w = await world(pg, tmp_path)
    _, lesson = await course_and_lesson(w)
    await write(w, "ivan", lesson)
    await write(w, "ivan", lesson, _variant(1, "n2"))
    assert await refusal(write(w, "ivan", lesson)) == "notes_cell_removed"
    assert await _counts(w) == (2, 2)
    await w.engine.dispose()


async def test_a_stage_can_make_an_earlier_revision_current(pg: Pg, tmp_path: Path) -> None:
    store = need("reader.lessons.store")
    w = await world(pg, tmp_path)
    _, lesson = await course_and_lesson(w)
    a, _ = await write(w, "ivan", lesson)
    b, _ = await write(w, "ivan", lesson, _variant(1))
    async with w.engine.connect() as conn:
        assert await store.current_sha256(conn, lesson) == b
    assert await write(w, "ivan", lesson, stage="revise") == (a, False)
    async with w.engine.connect() as conn:
        assert await store.current_sha256(conn, lesson) == a
    await w.engine.dispose()


async def test_missing_current_blob_fails_closed_until_healed(pg: Pg, tmp_path: Path) -> None:
    blobs = need("reader.blobs")
    w = await world(pg, tmp_path)
    _, lesson = await course_and_lesson(w)
    a, _ = await write(w, "ivan", lesson)
    (w.blobs / "sha256" / a[:2] / a).unlink()
    with pytest.raises(blobs.BlobMissing):
        await write(w, "ivan", lesson, _variant(1))
    assert await _counts(w) == (1, 1)
    assert await write(w, "ivan", lesson) == (a, False)  # rewriting the current revision heals it
    assert (await write(w, "ivan", lesson, _variant(1)))[1] is True
    await w.engine.dispose()


async def test_key_order_does_not_change_the_revision(pg: Pg, tmp_path: Path) -> None:
    def reverse(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: reverse(value[k]) for k in reversed(list(value))}
        if isinstance(value, list):
            return [reverse(v) for v in value]
        return value

    w = await world(pg, tmp_path)
    _, lesson = await course_and_lesson(w)
    reordered = json.dumps(reverse(json.loads(F01))).encode()
    assert reordered != F01
    assert await write(w, "ivan", lesson, reordered) == (F01_SHA, True)
    assert await write(w, "ivan", lesson) == (F01_SHA, False)
    await w.engine.dispose()


@pytest.mark.parametrize("form", ["upper", "braces", "hex32", "urn"])
async def test_only_canonical_lesson_ids_are_accepted(pg: Pg, tmp_path: Path, form: str) -> None:
    w = await world(pg, tmp_path)
    _, lesson = await course_and_lesson(w)
    text_id = {
        "upper": str(lesson).upper(),
        "braces": "{" + str(lesson) + "}",
        "hex32": lesson.hex,
        "urn": lesson.urn,
    }[form]
    store = need("reader.lessons.store")

    async def attempt() -> None:
        async with w.engine.begin() as conn:
            await store.write_revision(conn, w.blobs, w.users["ivan"], text_id, F01, "draft", {})

    assert await refusal(attempt()) == "unknown_lesson"
    assert (await write(w, "ivan", str(lesson)))[1] is True  # type: ignore[arg-type]
    await w.engine.dispose()


async def test_secret_in_provenance_is_a_type_error(pg: Pg, tmp_path: Path) -> None:
    from reader.config import Secret

    w = await world(pg, tmp_path)
    _, lesson = await course_and_lesson(w)
    with pytest.raises(TypeError, match="Secret"):
        await write(w, "ivan", lesson, provenance={"source": Secret("canary-7f3a")})
    assert await _counts(w) == (0, 0)
    await w.engine.dispose()


async def test_provenance_at_the_bound_is_accepted(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    _, lesson = await course_and_lesson(w)
    overhead = len(json.dumps({"source": ""}, separators=(",", ":")).encode())
    await write(w, "ivan", lesson, provenance={"source": "x" * (16384 - overhead)})
    assert await _counts(w) == (1, 1)
    await w.engine.dispose()


async def test_missing_blob_is_healed_by_a_rewrite(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    _, lesson = await course_and_lesson(w)
    sha, _ = await write(w, "ivan", lesson)
    blob = w.blobs / "sha256" / sha[:2] / sha
    blob.unlink()
    assert await write(w, "ivan", lesson) == (sha, False)
    assert blob.read_bytes() == F01
    await w.engine.dispose()


async def test_ensure_lesson_is_idempotent_and_owner_checked(pg: Pg, tmp_path: Path) -> None:
    store = need("reader.lessons.store")
    w = await world(pg, tmp_path)
    course, lesson = await course_and_lesson(w)
    async with w.engine.begin() as conn:
        again = await store.ensure_lesson(conn, w.users["ivan"], course, 1, "Another title")
    assert again == lesson
    assert await scalar(w.engine, "SELECT title FROM lessons WHERE id=:l", l=lesson) == "Lesson"

    async def intrude() -> None:
        async with w.engine.begin() as conn:
            await store.ensure_lesson(conn, w.users["anna"], course, 2, "Intruder")

    assert await refusal(intrude()) == "not_owner"
    await w.engine.dispose()


@pytest.mark.parametrize("title", ["", "   ", "x" * 201])
async def test_titles_are_bounded(pg: Pg, tmp_path: Path, title: str) -> None:
    store = need("reader.lessons.store")
    w = await world(pg, tmp_path)
    with pytest.raises(ValueError, match="title"):
        async with w.engine.begin() as conn:
            await store.create_course(conn, w.users["ivan"], title, None)
    await w.engine.dispose()


async def test_concurrent_writers_never_drop_notes(pg: Pg, tmp_path: Path) -> None:
    """Two connections each add a different notes cell to F01. Without the lesson lock both would see F01 as
    the newest revision and both would succeed, and the later revision would drop the other's notes cell."""
    w = await world(pg, tmp_path)
    _, lesson = await course_and_lesson(w)
    await write(w, "ivan", lesson)

    def with_notes(cell_id: str) -> bytes:
        doc = json.loads(F01)
        extra = json.loads(json.dumps(doc["cells"][8]))
        extra["id"] = cell_id
        doc["cells"].append(extra)
        return json.dumps(doc).encode()

    store = need("reader.lessons.store")

    async def attempt(data: bytes) -> str:
        async def run() -> None:
            async with w.engine.begin() as conn:
                await store.write_revision(conn, w.blobs, w.users["ivan"], lesson, data, "revise", {})
                await asyncio.sleep(0.2)  # hold the lesson lock while the other writer waits

        try:
            await run()
        except store.LessonWriteError as error:
            return str(error.code)
        return "ok"

    results = await asyncio.gather(attempt(with_notes("notes-a")), attempt(with_notes("notes-b")))
    assert sorted(results) == ["notes_cell_removed", "ok"]
    from sqlalchemy import text

    async with w.engine.connect() as conn:
        result = await conn.execute(text("SELECT sha256 FROM lesson_revision_stages ORDER BY id"))
        rows = result.scalars().all()
    previous: set[str] = set()
    for sha in rows:
        doc = json.loads((w.blobs / "sha256" / sha[:2] / sha).read_bytes())
        notes = {c["id"] for c in doc["cells"] if c["metadata"]["reader"]["kind"] == "notes"}
        assert previous <= notes
        previous = notes
    await w.engine.dispose()


async def test_revisions_and_stage_records_are_immutable(pg: Pg, tmp_path: Path) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    w = await world(pg, tmp_path)
    _, lesson = await course_and_lesson(w)
    await write(w, "ivan", lesson)
    for sql in (
        "UPDATE lesson_revisions SET byte_size = 1",
        "DELETE FROM lesson_revisions",
        "UPDATE lesson_revision_stages SET stage = 'draft'",
        "DELETE FROM lesson_revision_stages",
        "TRUNCATE lesson_revision_stages",
        "TRUNCATE lesson_revisions CASCADE",
    ):
        with pytest.raises(DBAPIError, match="immutable"):
            async with w.engine.begin() as conn:
                await conn.execute(text(sql))
    assert await _counts(w) == (1, 1)
    await w.engine.dispose()


async def test_owner_mismatch_is_refused_by_the_schema(pg: Pg, tmp_path: Path) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    w = await world(pg, tmp_path)
    course, lesson = await course_and_lesson(w, "ivan")
    await write(w, "ivan", lesson)
    params = {"anna": w.users["anna"], "c": course, "l": lesson, "s": F01_SHA}
    for sql in (
        "INSERT INTO lessons (id, owner_id, course_id, ordinal, title)"
        " VALUES (gen_random_uuid(), :anna, :c, 9, 't')",
        "INSERT INTO lesson_revisions (owner_id, lesson_id, sha256, byte_size, cell_count)"
        " VALUES (:anna, :l, repeat('a', 64), 1, 1)",
        "INSERT INTO lesson_revision_stages (owner_id, lesson_id, sha256, stage, provenance)"
        " VALUES (:anna, :l, :s, 'draft', '{}')",
        "UPDATE lessons SET published_sha256 = repeat('b', 64) WHERE id = :l",
    ):
        with pytest.raises(IntegrityError, match="foreign key"):
            async with w.engine.begin() as conn:
                await conn.execute(text(sql), params)
    await w.engine.dispose()


async def test_migration_creates_and_drops_the_tables(pg: Pg, tmp_path: Path) -> None:
    from alembic import command

    from reader.db import alembic_config

    w = await world(pg, tmp_path)
    assert await scalar(w.engine, TABLES) == 4
    learner = (
        "SELECT count(*) FROM pg_tables"
        " WHERE tablename IN ('lesson_progress', 'lesson_notes', 'lesson_bookmarks')"
    )
    assert await scalar(w.engine, learner) == 3

    def downgrade(connection: Any) -> None:
        config = alembic_config()
        config.attributes["connection"] = connection
        command.downgrade(config, "lessons@-1")

    packs = "SELECT count(*) FROM pg_tables WHERE tablename = 'revision_packs'"
    assert await scalar(w.engine, packs) == 1
    # r3_gates_revision: lessons_0003 (FR-LESSON-32) is the branch head; its downgrade drops only revision_packs.
    async with w.engine.connect() as conn:
        await conn.run_sync(downgrade)
        await conn.commit()
    assert (await scalar(w.engine, packs), await scalar(w.engine, learner), await scalar(w.engine, TABLES)) == (
        0,
        3,
        4,
    )
    # Amendment V/manage: lessons_0002 (FR-LESSON-20); its downgrade drops only its tables.
    async with w.engine.connect() as conn:
        await conn.run_sync(downgrade)
        await conn.commit()
    assert (await scalar(w.engine, learner), await scalar(w.engine, TABLES)) == (0, 4)
    async with w.engine.connect() as conn:
        await conn.run_sync(downgrade)
        await conn.commit()
    assert await scalar(w.engine, TABLES) == 0
    assert await scalar(w.engine, "SELECT count(*) FROM pg_tables WHERE tablename = 'users'") == 1
    await w.engine.dispose()


# FR-LESSON-32: the packs a revision cites (r3_gates_revision). Synthetic material and span texts, no book text.
MATERIAL = hashlib.sha256(b"lessons r3 gates").hexdigest()
SPANS = [(1, "text", "Synthetic span one."), (1, "text", "Synthetic span two."), (2, "text", "Synthetic span three.")]


async def _material(w: Any, legacy_active: bool, with_r3: bool) -> tuple[str, str | None]:
    """The material's r2 legacy pack and, optionally, its r3 pack (active when present)."""
    from sqlalchemy import text

    from ingest.packseed import R2, R3, add_pack

    owner = w.users["ivan"]
    async with w.engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO materials (owner_id, sha256, config_id, page_count) VALUES (:o, :s, 'm', 2)"),
            {"o": owner, "s": MATERIAL},
        )
        legacy = await add_pack(conn, owner, MATERIAL, R2, SPANS, activate=legacy_active)
        r3 = await add_pack(conn, owner, MATERIAL, R3, SPANS, activate=True) if with_r3 else None
    return legacy, r3


async def _locators(w: Any, pack: str) -> list[tuple[str, str]]:
    rows = await _rows(
        w,
        "SELECT locator, quote_sha256 FROM material_spans"
        " WHERE ingest_id = (SELECT ingest_id FROM base_packs WHERE pack_id = :p) ORDER BY seq",
        p=pack,
    )
    return [(str(r[0]), str(r[1])) for r in rows]


async def _rows(w: Any, sql: str, **params: Any) -> list[Any]:
    from sqlalchemy import text

    async with w.engine.connect() as conn:
        return list((await conn.execute(text(sql), params)).all())


async def _cited(w: Any, lesson: uuid.UUID, sha: str) -> list[str]:
    rows = await _rows(w, "SELECT pack_id FROM revision_packs WHERE lesson_id = :l AND sha256 = :s", l=lesson, s=sha)
    return sorted(str(r[0]) for r in rows)


def _citing(where: str, ref: tuple[str, str], legacy: str | None = None) -> bytes:
    """F01 with one locator reference (and its hash) at `where`; optionally a legacy id in the erratum."""
    doc = json.loads(F01)
    cells = doc["cells"]
    span, quote = ref
    if where == "claim":
        cells[2]["metadata"]["reader"]["claims"][0]["citations"].append(
            {"span": span, "quote_sha256": quote, "page": 1}
        )
    elif where == "source_refs":
        cells[1]["metadata"]["reader"]["source_refs"].append(
            {"material_sha256": MATERIAL, "page": 1, "span": span, "quote_sha256": quote}
        )
    elif where == "erratum":
        cells[4]["metadata"]["reader"]["source_ref"] = {
            "material_sha256": MATERIAL,
            "page": 1,
            "span": span,
            "quote_sha256": quote,
        }
    else:
        cells[6]["metadata"]["reader"]["reference"] = {
            "kind": "test_vector",
            "span": span,
            "quote_sha256": quote,
            "expected": ["0.628"],
        }
    if legacy is not None:
        cells[4]["metadata"]["reader"]["source_ref"]["span"] = legacy
    return json.dumps(doc, ensure_ascii=False).encode()


async def test_a_revision_records_the_active_pack_it_cites(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    legacy, r3 = await _material(w, legacy_active=False, with_r3=True)
    assert r3 is not None
    locs = await _locators(w, r3)
    assert locs == await _locators(w, legacy)  # the backfilled legacy pack holds r3's (locator, hash)
    _, lesson = await course_and_lesson(w)

    doc = json.loads(_citing("claim", locs[0], legacy=f"{MATERIAL}:1"))
    doc["cells"][2]["metadata"]["reader"]["claims"][0]["citations"].append(
        {"span": locs[2][0], "quote_sha256": locs[2][1], "page": 2}
    )
    two = json.dumps(doc).encode()
    sha, created = await write(w, "ivan", lesson, two)
    assert created
    assert await _cited(w, lesson, sha) == [r3]  # one row, r3 only; the legacy id adds none

    again, created = await write(w, "ivan", lesson, two, stage="review")
    assert (again, created) == (sha, False)
    assert await _cited(w, lesson, sha) == [r3]  # rewriting the same bytes adds none

    for where in ("source_refs", "erratum", "lab"):  # every place a span reference can stand
        one, created = await write(w, "ivan", lesson, _citing(where, locs[1]))
        assert created
        assert await _cited(w, lesson, one) == [r3], where

    wrong, created = await write(w, "ivan", lesson, _citing("claim", (locs[0][0], "0" * 64)))
    assert created
    assert await _cited(w, lesson, wrong) == []  # a hash that differs from the stored span's names no pack

    f01, _ = await write(w, "ivan", lesson, F01)
    assert await _cited(w, lesson, f01) == []  # the fixture's synthetic material has no pack

    store = need("reader.lessons.store")
    late = _citing("claim", locs[2])
    with pytest.raises(RuntimeError, match="rollback"):
        async with w.engine.begin() as conn:
            rolled, _ = await store.write_revision(conn, w.blobs, w.users["ivan"], lesson, late, "draft", {})
            raise RuntimeError("rollback")
    assert await _cited(w, lesson, rolled) == []  # written in the revision's transaction
    await w.engine.dispose()


async def test_with_the_legacy_pack_active_the_legacy_pack_is_recorded(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    legacy, _ = await _material(w, legacy_active=True, with_r3=False)
    locs = await _locators(w, legacy)
    _, lesson = await course_and_lesson(w)
    sha, created = await write(w, "ivan", lesson, _citing("claim", locs[0]))
    assert created
    assert await _cited(w, lesson, sha) == [legacy]
    legacy_only, _ = await write(w, "ivan", lesson, _citing("claim", locs[0], legacy=f"{MATERIAL}:0"))
    assert await _cited(w, lesson, legacy_only) == [legacy]  # the locator's row; the legacy id adds none
    await w.engine.dispose()


async def test_revision_packs_rows_are_immutable(pg: Pg, tmp_path: Path) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    w = await world(pg, tmp_path)
    legacy, _ = await _material(w, legacy_active=True, with_r3=False)
    _, lesson = await course_and_lesson(w)
    sha, _ = await write(w, "ivan", lesson, _citing("claim", (await _locators(w, legacy))[0]))
    for sql in (
        "UPDATE revision_packs SET pack_id = repeat('a', 64)",
        "DELETE FROM revision_packs",
        "TRUNCATE revision_packs",
    ):
        with pytest.raises(DBAPIError, match="immutable"):
            async with w.engine.begin() as conn:
                await conn.execute(text(sql))
    assert await _cited(w, lesson, sha) == [legacy]
    await w.engine.dispose()
