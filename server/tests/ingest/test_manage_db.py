"""Rename, soft delete and restore of imported materials (SRV-INGEST FR-INGEST-30..32, [db]).

@trace FR-INGEST-30 FR-INGEST-31 FR-INGEST-32

The course cascade calls SRV-LESSON's `reader.lessons.manage` (built on V/manage-impl-lesson). The tests
under "the cascade" fail with "reader.lessons.manage missing" until that branch is merged; every other test
runs here. Before that merge the `courses` and `lessons` tables lack SRV-LESSON's manage columns, so
`course_columns` adds the ones these tests read with IF NOT EXISTS (a no-op once SRV-LESSON's migration is in
the heads).
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import unicodedata
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from . import conftest
from .conftest import MATERIAL_SHA, ORIGIN, SESSION_COOKIE, Pg, World, client, need, session_for, world

pytestmark = pytest.mark.anyio

SYN = "synthetic"  # reader.yaml id of MATERIAL_SHA in the ingest world
OWNED_TABLES = ("materials", "ingests", "material_spans", "material_outline", "uploads")


def lessons_manage() -> ModuleType:
    """SRV-LESSON's manage functions: the cascade's dependency (merged from V/manage-impl-lesson)."""
    try:
        return importlib.import_module("reader.lessons.manage")
    except ModuleNotFoundError:
        raise AssertionError("reader.lessons.manage missing (SRV-LESSON manage slice not merged)") from None


# ---- fixtures as rows ----


async def execute(w: World, sql: str, **params: Any) -> None:
    from sqlalchemy import text

    async with w.engine.begin() as conn:
        await conn.execute(text(sql), params)


async def course_columns(w: World) -> None:
    """Pre-merge shim from conftest (also run by `world`); a no-op after the merge."""
    await conftest.course_columns(w.engine)


async def imported(
    w: World, config_id: str = SYN, sha: str = MATERIAL_SHA, handle: str = "ivan"
) -> uuid.UUID:
    """An imported material (materials, ingests, one span, one outline entry) written as rows."""
    from sqlalchemy import text

    owner = w.users[handle]
    async with w.engine.begin() as conn:
        material = (
            await conn.execute(
                text(
                    "INSERT INTO materials (owner_id, sha256, config_id, page_count) VALUES (:o, :s, :c, 2)"
                    " RETURNING id"
                ),
                {"o": owner, "s": sha, "c": config_id},
            )
        ).scalar_one()
        ingest = (
            await conn.execute(
                text(
                    "INSERT INTO ingests (owner_id, material_id, converter_version, artifact_id, span_count,"
                    " outline_count, outline_source, pages_without_text)"
                    " VALUES (:o, :m, 'test-1', :a, 1, 1, 'bookmarks', '{}') RETURNING id"
                ),
                {"o": owner, "m": material, "a": "d" * 64},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO material_spans (ingest_id, owner_id, seq, page, kind, text, x0, y0, x1, y1)"
                " VALUES (:i, :o, 0, 1, 'text', 'Synthetic words.', 0, 0, 1, 1)"
            ),
            {"i": ingest, "o": owner},
        )
        await conn.execute(
            text(
                "INSERT INTO material_outline (ingest_id, owner_id, seq, level, title, page)"
                " VALUES (:i, :o, 0, 1, 'Part', 1)"
            ),
            {"i": ingest, "o": owner},
        )
    result: uuid.UUID = material
    return result


async def add_course(
    w: World, title: str, sha: str | None = MATERIAL_SHA, *, state: str = "active", lessons: int = 1
) -> uuid.UUID:
    """A course of ivan in state active (positioned), archived or deleted, with `lessons` lessons."""
    from sqlalchemy import text

    owner = w.users["ivan"]
    async with w.engine.begin() as conn:
        position = (
            await conn.execute(
                text("SELECT coalesce(max(position), 0) + 1 FROM courses WHERE owner_id = :o"), {"o": owner}
            )
        ).scalar_one()
        course = (
            await conn.execute(
                text(
                    "INSERT INTO courses"
                    " (owner_id, title, material_sha256, position, archived_at, deleted_at)"
                    " VALUES (:o, :t, :s, :p,"
                    " CASE WHEN :st = 'archived' THEN now() END, CASE WHEN :st = 'deleted' THEN now() END)"
                    " RETURNING id"
                ),
                {"o": owner, "t": title, "s": sha, "p": position if state == "active" else None, "st": state},
            )
        ).scalar_one()
        for ordinal in range(1, lessons + 1):
            await conn.execute(
                text(
                    "INSERT INTO lessons (owner_id, course_id, ordinal, title) VALUES (:o, :c, :n, :t)"
                ),
                {"o": owner, "c": course, "n": ordinal, "t": f"Lesson {ordinal}"},
            )
    result: uuid.UUID = course
    return result


async def add_owner(w: World, handle: str = "olga") -> None:
    await execute(
        w,
        "INSERT INTO users"
        " (handle, role, webauthn_user_id, display_name, consent_evaluation, handle_skeleton)"
        " VALUES (:h, 'owner', :u, :h, false, :h)",
        h=handle,
        u=os.urandom(64),
    )
    w.users[handle] = await w.scalar("SELECT id FROM users WHERE handle = :h", h=handle)


async def audit(w: World) -> list[tuple[str, str, dict[str, Any]]]:
    rows = await w.rows("SELECT action, object_id, detail FROM audit_log ORDER BY id")
    return [(r[0], r[1], r[2] if isinstance(r[2], dict) else json.loads(r[2])) for r in rows]


async def counts(w: World) -> dict[str, int]:
    return {t: int(await w.scalar(f"SELECT count(*) FROM {t}")) for t in OWNED_TABLES}  # noqa: S608


async def material_state(w: World, sha: str = MATERIAL_SHA) -> tuple[Any, Any, int]:
    row = (await w.rows("SELECT deleted_at, title, version FROM materials WHERE sha256 = :s", s=sha))[0]
    return row[0], row[1], int(row[2])


async def delete(c: httpx.AsyncClient, material_id: str, body: Any) -> httpx.Response:
    return await c.request("DELETE", f"/api/materials/{material_id}", json=body)


async def running_key(w: World, kind: str, key: str) -> None:
    """A key whose procrastinate job is `doing` (SRV-EVENTS job_status `running`)."""
    await execute(
        w, "INSERT INTO job_keys (kind, key, args, attempts) VALUES (:k, :key, '{}', 1)", k=kind, key=key
    )
    await execute(
        w,
        "INSERT INTO procrastinate_jobs (queue_name, task_name, lock, args, status)"
        " VALUES ('ingest', :k, :key, '{}', 'todo')",
        k=kind,
        key=key,
    )
    await execute(w, "UPDATE procrastinate_jobs SET status = 'doing' WHERE lock = :key", key=key)


@asynccontextmanager
async def foreign(w: World) -> AsyncIterator[httpx.AsyncClient]:
    """The owner's session with another Origin (FR-AUTH-12)."""
    from reader.api.app import create_app

    app = create_app(w.settings, w.engine, run_cleanup_task=False)
    app.state.blobs_root = w.blobs
    app.state.ingest_materials = w.config.materials
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=ORIGIN,
        headers={"Origin": "https://evil.example"},
        cookies={SESSION_COOKIE: await session_for(w, "ivan")},
    ) as c:
        yield c


def by_id(listing: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {m["id"]: m for m in listing["materials"]}


# ---- GET /api/materials (FR-INGEST-15 amendment, FR-INGEST-23 display title) ----


async def test_listing_adds_title_version_courses_and_hides_deleted(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        await course_columns(w)
        await imported(w)
        active = await add_course(w, "Active course")
        archived = await add_course(w, "Archived course", state="archived")
        await add_course(w, "Deleted course", state="deleted")
        await add_course(w, "Other material", sha="b" * 64)
        async with client(w) as c:
            listed = by_id((await c.get("/api/materials")).json())
            syn, other = listed[SYN], listed["other"]
            assert (syn["title"], syn["version"], syn["deleted_at"]) == (SYN, 1, None)
            expected = {(str(active), "Active course"), (str(archived), "Archived course")}
            assert {(x["course_id"], x["title"]) for x in syn["courses"]} == expected
            assert all(set(x) == {"course_id", "title"} for x in syn["courses"])
            assert (other["title"], other["version"], other["deleted_at"]) == ("other", None, None)
            await execute(w, "UPDATE materials SET deleted_at = now() WHERE sha256 = :s", s=MATERIAL_SHA)
            default = (await c.get("/api/materials")).json()
            deleted = (await c.get("/api/materials?state=deleted")).json()
            bogus = await c.get("/api/materials?state=bogus")
        assert [m["id"] for m in default["materials"]] == ["other"]
        assert [m["id"] for m in deleted["materials"]] == [SYN]
        assert deleted["materials"][0]["deleted_at"] is not None
        assert (bogus.status_code, bogus.json()) == (400, {"error": "state"})


# ---- FR-INGEST-30: rename ----


async def test_rename_cases(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        await course_columns(w)
        await imported(w)
        url = f"/api/materials/{SYN}"
        async with client(w) as c:
            first = await c.post(url, json={"version": 1, "title": "  Новое имя  "})
            again = await c.post(url, json={"version": 1, "title": "Новое имя"})
            stale = await c.post(url, json={"version": 1, "title": "Другое"})
            nfd = unicodedata.normalize("NFD", "Йога")
            renamed = await c.post(url, json={"version": 2, "title": nfd})
            cleared = await c.post(url, json={"version": 3, "title": None})
            refusals = [
                await c.post(url, json={"version": 4, "title": t})
                for t in ("", "   ", "x" * 201, "a\u0007b", 5)
            ]
            malformed = [
                await c.post(url, json=b) for b in ({"title": "x"}, {"version": "4", "title": "x"}, [1])
            ]
            never = await c.post("/api/materials/other", json={"version": 1, "title": "x"})
            unknown = await c.post("/api/materials/nope", json={"version": 1, "title": "x"})
            listed = by_id((await c.get("/api/materials")).json())[SYN]
        assert first.status_code == 200, first.text
        assert (first.json()["id"], first.json()["title"], first.json()["version"]) == (SYN, "Новое имя", 2)
        assert first.json()["sha256"] == MATERIAL_SHA and first.json()["source"] == "config"
        assert (again.status_code, again.json()) == (200, first.json())  # no-op before the version check
        assert stale.status_code == 409
        assert stale.json() == {"error": "stale", "material": first.json()}
        assert renamed.status_code == 200 and renamed.json()["title"] == "Йога"
        assert unicodedata.is_normalized("NFC", renamed.json()["title"])
        assert (cleared.status_code, cleared.json()["title"], cleared.json()["version"]) == (200, SYN, 4)
        assert [(r.status_code, r.json()) for r in refusals] == [(400, {"error": "title_invalid"})] * 5
        assert [r.status_code for r in malformed] == [400, 400, 400]
        assert (never.status_code, never.json()) == (404, {"error": "not_found"})
        assert unknown.status_code == 404
        assert listed == cleared.json()
        assert await material_state(w) == (None, None, 4)
        assert await audit(w) == [
            ("material.rename", SYN, {"version": 2}),
            ("material.rename", SYN, {"version": 3}),
            ("material.rename", SYN, {"version": 4}),
        ]
        await execute(w, "UPDATE materials SET deleted_at = now()")
        async with client(w) as c:
            gone = await c.post(url, json={"version": 4, "title": "x"})
        assert (gone.status_code, gone.json()) == (404, {"error": "not_found"})


async def test_rename_of_an_uploaded_material_keeps_its_id(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        await course_columns(w)
        body_sha = hashlib.sha256(b"upload-body").hexdigest()
        up_id = f"up-{body_sha[:12]}"
        await execute(
            w,
            "INSERT INTO uploads (owner_id, sha256, size, filename, language, state)"
            " VALUES (:o, :s, 10, 'My Book.PDF', 'en', 'received')",
            o=w.users["ivan"],
            s=body_sha,
        )
        await imported(w, config_id=up_id, sha=body_sha)
        async with client(w) as c:
            before = by_id((await c.get("/api/materials")).json())[up_id]
            renamed = await c.post(f"/api/materials/{up_id}", json={"version": 1, "title": "Shelf name"})
            after = by_id((await c.get("/api/materials")).json())[up_id]
        assert before["title"] == "My Book"
        assert renamed.status_code == 200, renamed.text
        assert (after["id"], after["title"], after["version"], after["source"]) == (
            up_id, "Shelf name", 2, "upload",
        )


# ---- FR-INGEST-31: impact, delete, restore ----


async def test_impact_counts_courses_lessons_and_open_uploads(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        await course_columns(w)
        await imported(w)
        active = await add_course(w, "Active course", lessons=2)
        archived = await add_course(w, "Archived course", state="archived", lessons=1)
        await add_course(w, "Deleted course", state="deleted", lessons=4)
        async with client(w) as c:
            impact = await c.get(f"/api/materials/{SYN}/impact")
            never = await c.get("/api/materials/other/impact")
        assert impact.status_code == 200, impact.text
        body = impact.json()
        assert set(body) == {"courses", "lessons", "uploads_open"}
        expected = {(str(active), "Active course"), (str(archived), "Archived course")}
        assert {(x["course_id"], x["title"]) for x in body["courses"]} == expected
        assert (body["lessons"], body["uploads_open"]) == (3, 0)
        assert never.status_code == 404


async def test_delete_confirm_rules(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        await course_columns(w)
        await imported(w)
        long = "Д" * 41
        async with client(w) as c:
            renamed = await c.post(f"/api/materials/{SYN}", json={"version": 1, "title": long})
            assert renamed.status_code == 200
            wrong = await delete(c, SYN, {"confirm": "Д" * 39})
            missing = await delete(c, SYN, {})
            assert await material_state(w) == (None, long, 2)
            first40 = await delete(c, SYN, {"confirm": "  " + "Д" * 40 + " "})
        assert (wrong.status_code, wrong.json()) == (400, {"error": "confirm_mismatch"})
        assert missing.status_code == 400
        assert first40.status_code == 200, first40.text
        await execute(w, "UPDATE materials SET deleted_at = NULL, title = 'Йога'")
        async with client(w) as c:
            nfd = await delete(c, SYN, {"confirm": unicodedata.normalize("NFD", "Йога")})
        assert nfd.status_code == 200, nfd.text


async def test_delete_confirm_target_is_cut_to_40_then_trimmed(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        await course_columns(w)
        await imported(w)
        await execute(w, "UPDATE materials SET title = :t", t="a" * 39 + " tail words")  # target: "a" * 39
        async with client(w) as c:
            refused = await delete(c, SYN, {"confirm": "a" * 39 + " t"})
            assert (refused.status_code, refused.json()) == (400, {"error": "confirm_mismatch"})
            assert (await material_state(w))[0] is None
            deleted = await delete(c, SYN, {"confirm": "a" * 39})
        assert deleted.status_code == 200, deleted.text


NON_SLUG = {"Bashir": "c" * 64, "bashir_4th": "e" * 64}  # ids SRV-CONFIG FR-CONFIG-18 accepts


async def test_non_slug_reader_yaml_ids_are_renamed_deleted_and_audited(pg: Pg, tmp_path: Path) -> None:
    from reader.config import load_config

    async with world(pg, tmp_path) as w:
        await course_columns(w)
        rows = "".join(
            f'  - {{ id: {mid}, file: ./{mid}.pdf, sha256: "{sha}",'
            " license: personal-use, language: ru }\n"
            for mid, sha in NON_SLUG.items()
        )
        path = tmp_path / "non-slug.yaml"
        path.write_text(
            'version: 1\ninstance: { url: "https://localhost", data_dir: ./data }\n'
            "users:\n  - { handle: ivan, role: owner }\n  - { handle: anna, role: learner }\n"
            "materials:\n" + rows + "sharing: {}\n",
            encoding="utf-8",
        )
        w.config = load_config(path)
        for mid, sha in NON_SLUG.items():
            await imported(w, config_id=mid, sha=sha)
        async with client(w) as c:
            for mid in NON_SLUG:
                renamed = await c.post(f"/api/materials/{mid}", json={"version": 1, "title": f"{mid} book"})
                assert renamed.status_code == 200, renamed.text
                deleted = await delete(c, mid, {"confirm": f"{mid} book"})
                assert deleted.status_code == 200, deleted.text
        assert await audit(w) == [
            ("material.rename", "Bashir", {"version": 2}),
            ("material.delete", "Bashir", {"courses": 0}),
            ("material.rename", "bashir_4th", {"version": 2}),
            ("material.delete", "bashir_4th", {"courses": 0}),
        ]


async def test_a_refused_audit_row_answers_audit_refused_and_writes_nothing(
    pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from reader.jobs import audit as audit_mod

    async with world(pg, tmp_path) as w:
        await course_columns(w)
        await imported(w)
        monkeypatch.setitem(audit_mod._ID_RULES, "material", lambda value: False)
        with caplog.at_level(logging.ERROR, logger="reader.jobs.audit"):
            async with client(w) as c:
                renamed = await c.post(f"/api/materials/{SYN}", json={"version": 1, "title": "Secret title"})
        assert (renamed.status_code, renamed.json()) == (500, {"error": "audit_refused"})
        assert await material_state(w) == (None, None, 1)  # rolled back with the refused row
        assert await audit(w) == []
        logged = [r.getMessage() for r in caplog.records if r.name == "reader.jobs.audit"]
        assert logged == ["audit_refused reason=object_id"]


async def test_delete_in_use_without_cascade_writes_nothing(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        await course_columns(w)
        await imported(w)
        active = await add_course(w, "Active course")
        archived = await add_course(w, "Archived course", state="archived")
        before = await counts(w)
        async with client(w) as c:
            plain = await delete(c, SYN, {"confirm": SYN})
            explicit = await delete(c, SYN, {"confirm": SYN, "cascade": False})
        for response in (plain, explicit):
            assert response.status_code == 409
            body = response.json()
            assert body["error"] == "material_in_use"
            expected = {(str(active), "Active course"), (str(archived), "Archived course")}
            assert {(x["course_id"], x["title"]) for x in body["courses"]} == expected
        assert (await material_state(w))[0] is None
        assert await counts(w) == before
        assert await audit(w) == []
        assert await w.scalar("SELECT count(*) FROM courses WHERE deleted_at IS NOT NULL") == 0


async def test_delete_busy_cases(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        await course_columns(w)
        material = await imported(w)
        uploads = need("reader.ingest.uploads")

        async def refused() -> None:
            async with client(w) as c:
                response = await delete(c, SYN, {"confirm": SYN})
            assert (response.status_code, response.json()) == (409, {"error": "material_busy"})
            assert (await material_state(w))[0] is None

        # an open upload of the material's bytes (no import key)
        await execute(
            w,
            "INSERT INTO uploads (owner_id, sha256, size, filename, language, state)"
            " VALUES (:o, :s, 10, 'x.pdf', 'ru', 'queued')",
            o=w.users["ivan"],
            s=MATERIAL_SHA,
        )
        async with client(w) as c:
            assert (await c.get(f"/api/materials/{SYN}/impact")).json()["uploads_open"] == 1
        await refused()
        await execute(w, "DELETE FROM uploads")
        # a running import key
        async with w.engine.connect() as conn:
            import_key = await uploads.import_key(conn, MATERIAL_SHA)
            embed_key = await uploads.embed_key(conn, MATERIAL_SHA)
        assert embed_key is not None
        await running_key(w, "ingest.import", import_key)
        await refused()
        await execute(w, "DELETE FROM procrastinate_jobs")
        await execute(w, "DELETE FROM job_keys")
        # a running embed key
        await running_key(w, "search.embed", embed_key)
        await refused()
        await execute(w, "DELETE FROM procrastinate_jobs")
        await execute(w, "DELETE FROM job_keys")
        # a running freshness report of the material (SRV-FRESH FR-FRESH-20)
        ingest = await w.scalar("SELECT id FROM ingests WHERE material_id = :m", m=material)
        await execute(
            w,
            "INSERT INTO freshness_reports"
            " (owner_id, material_id, ingest_id, status, scan_inputs_sha256, sample)"
            " VALUES (:o, :m, :i, 'running', :s, '[]')",
            o=w.users["ivan"],
            m=material,
            i=ingest,
            s="c" * 64,
        )
        await refused()
        await execute(w, "UPDATE freshness_reports SET status = 'done'")
        async with client(w) as c:
            assert (await delete(c, SYN, {"confirm": SYN})).status_code == 200
        assert await audit(w) == [("material.delete", SYN, {"courses": 0})]


async def test_delete_then_restore_an_unused_material(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        await course_columns(w)
        await imported(w)
        async with client(w) as c:
            first = await delete(c, SYN, {"confirm": SYN})
            second = await delete(c, SYN, {"confirm": SYN})
            hidden = [m["id"] for m in (await c.get("/api/materials")).json()["materials"]]
            shown = [m["id"] for m in (await c.get("/api/materials?state=deleted")).json()["materials"]]
            reads = [
                await c.get(f"/api/materials/{SYN}/outline"),
                await c.get(f"/api/materials/{SYN}/spans?page=1"),
                await c.get(f"/api/materials/{SYN}/impact"),
                await c.post(f"/api/materials/{SYN}", json={"version": 1, "title": "x"}),
            ]
            restored = await c.post(f"/api/materials/{SYN}/restore", json={})
            again = await c.post(f"/api/materials/{SYN}/restore", json={})
            outline = await c.get(f"/api/materials/{SYN}/outline")
            listed = [m["id"] for m in (await c.get("/api/materials")).json()["materials"]]
        assert first.status_code == 200, first.text
        body = first.json()
        assert (body["courses"], body["lessons"], body["uploads_open"]) == ([], 0, 0)
        assert body["deleted_at"] is not None
        assert (second.status_code, second.json()) == (200, body)
        assert hidden == ["other"] and shown == [SYN]
        assert [(r.status_code, r.json()) for r in reads] == [(404, {"error": "not_found"})] * 4
        assert restored.status_code == 200, restored.text
        assert restored.json()["restored_courses"] == []
        assert restored.json()["material"]["id"] == SYN
        assert restored.json()["material"]["deleted_at"] is None
        assert again.status_code == 200
        assert outline.status_code == 200 and listed == [SYN, "other"]
        assert await audit(w) == [
            ("material.delete", SYN, {"courses": 0}),
            ("material.restore", SYN, {"courses": 0}),
        ]


async def test_delete_and_restore_of_a_material_that_is_not_imported(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        await course_columns(w)
        async with client(w) as c:
            gone = await delete(c, "other", {"confirm": "other"})
            back = await c.post("/api/materials/other/restore", json={})
        assert (gone.status_code, back.status_code) == (404, 404)


# ---- FR-INGEST-32: tombstones only, owner only ----


def blob_listing(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


async def test_a_delete_restore_cycle_removes_no_row_and_no_file(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        await course_columns(w)
        await imported(w)
        artifact = w.blobs / "ingest" / ("d" * 64)
        artifact.mkdir(parents=True)
        (artifact / "manifest.json").write_bytes(b"{}")
        inbox = w.blobs / "inbox"
        inbox.mkdir()
        (inbox / f"{MATERIAL_SHA}.pdf").write_bytes(b"%PDF-1.7\n%%EOF\n")
        rows_before, files_before = await counts(w), blob_listing(w.blobs)
        async with client(w) as c:
            assert (await delete(c, SYN, {"confirm": SYN})).status_code == 200
            assert await counts(w) == rows_before and blob_listing(w.blobs) == files_before
            assert (await c.post(f"/api/materials/{SYN}/restore", json={})).status_code == 200
        assert await counts(w) == rows_before
        assert blob_listing(w.blobs) == files_before


async def test_a_delete_restore_cycle_appends_two_manifest_entries(pg: Pg, tmp_path: Path) -> None:
    """FR-INGEST-31 revision 3: delete appends one entry, restore one, back to the same brain_version."""
    from .test_overlays_db import activate
    from .test_packs_db import imported as imported_pack

    async with world(pg, tmp_path) as w:
        await course_columns(w)
        await activate(w, await imported_pack(w))
        async with client(w) as c:
            assert (await delete(c, SYN, {"confirm": SYN})).status_code == 200
            assert (await c.post(f"/api/materials/{SYN}/restore", json={})).status_code == 200
        rows = await w.rows("SELECT seq, brain_version, entry FROM manifest_entries ORDER BY seq")
        assert [r[0] for r in rows] == [1, 2, 3]
        assert rows[1][2]["packs"] == [] and rows[2][1] == rows[0][1]


async def test_owner_only_matrix_over_the_four_routes(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        await course_columns(w)
        await imported(w)
        await add_owner(w)

        async def calls(c: httpx.AsyncClient) -> list[httpx.Response]:
            return [
                await c.post(f"/api/materials/{SYN}", json={"version": 1, "title": "x"}),
                await c.get(f"/api/materials/{SYN}/impact"),
                await delete(c, SYN, {"confirm": SYN}),
                await c.post(f"/api/materials/{SYN}/restore", json={}),
            ]

        before = await counts(w)
        async with client(w, "anna") as c:
            learner = await calls(c)
        async with client(w, "olga") as c:
            second = await calls(c)
        async with foreign(w) as c:
            crossed = await calls(c)
        assert [(r.status_code, r.json()) for r in learner] == [(403, {"error": "owner_only"})] * 4
        assert [(r.status_code, r.json()) for r in second] == [(404, {"error": "not_found"})] * 4
        assert [r.status_code for r in crossed] == [403, 200, 403, 403]  # the GET passes FR-AUTH-12
        assert [r.json() for r in crossed if r.status_code == 403] == [{"error": "origin"}] * 3
        assert await counts(w) == before
        assert await material_state(w) == (None, None, 1)
        assert await audit(w) == []


# ---- import and upload of a deleted material (FR-INGEST-15 amendment) ----


async def test_upload_of_a_deleted_material_is_refused(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        await course_columns(w)
        body = b"%PDF-1.7\ndeleted\n" + b"x" * 2000 + b"%%EOF\n"
        body_sha = hashlib.sha256(body).hexdigest()
        await imported(w, config_id=f"up-{body_sha[:12]}", sha=body_sha)
        await execute(w, "UPDATE materials SET deleted_at = now()")
        async with client(w) as c:
            response = await c.post(
                "/api/materials/uploads?language=ru",
                content=body,
                headers={"Content-Type": "application/pdf", "X-Reader-Filename": "Book.pdf"},
            )
        assert (response.status_code, response.json()) == (409, {"error": "material_deleted"})
        assert await w.scalar("SELECT count(*) FROM uploads") == 0
        assert sorted(p.name for p in (w.blobs / "inbox").iterdir()) == []


async def test_import_of_a_deleted_material_does_nothing(
    pg: Pg, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from .test_ingest_db import receive, tar

    async with world(pg, tmp_path) as w:
        await course_columns(w)
        from sqlalchemy import text

        async with w.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO materials (owner_id, sha256, config_id, page_count, deleted_at)"
                    " VALUES (:o, :s, :c, 2, now())"
                ),
                {"o": w.users["ivan"], "s": MATERIAL_SHA, "c": SYN},
            )
        await receive(w, tar())
        with caplog.at_level(logging.INFO, logger="reader.ingest"):
            await w.work()
        assert await w.scalar("SELECT count(*) FROM ingests") == 0
        assert await w.scalar("SELECT deleted_at IS NOT NULL FROM materials") is True
        assert any(f"ingest material_deleted {SYN}" in r.getMessage() for r in caplog.records)


# ---- the cascade (needs SRV-LESSON's reader.lessons.manage: runs after the merge) ----


async def test_cascade_deletes_its_courses_and_restore_brings_them_back(pg: Pg, tmp_path: Path) -> None:
    lessons_manage()
    async with world(pg, tmp_path) as w:
        await course_columns(w)
        await imported(w)
        active = await add_course(w, "Active course", lessons=2)
        archived = await add_course(w, "Archived course", state="archived")
        unrelated = await add_course(w, "Unrelated", sha="b" * 64)
        async with client(w) as c:
            deleted = await delete(c, SYN, {"confirm": SYN, "cascade": True})
            again = await delete(c, SYN, {"confirm": SYN, "cascade": True})
        assert deleted.status_code == 200, deleted.text
        body = deleted.json()
        expected = {(str(active), "Active course"), (str(archived), "Archived course")}
        assert {(x["course_id"], x["title"]) for x in body["courses"]} == expected
        assert body["lessons"] == 3
        assert (again.status_code, again.json()) == (200, body)
        rows = await w.rows("SELECT id, deleted_at IS NOT NULL, deleted_by_material, position FROM courses")
        state = {r[0]: (r[1], r[2], r[3]) for r in rows}
        assert state[active][:2] == (True, SYN) and state[archived][:2] == (True, SYN)
        assert state[unrelated][:2] == (False, None)
        live = await w.scalar(
            "SELECT count(*) FROM courses c JOIN materials m ON m.sha256 = c.material_sha256"
            " WHERE c.deleted_at IS NULL AND m.deleted_at IS NOT NULL"
        )
        assert live == 0  # FR-INGEST-32
        actions = [a for a, _, _ in await audit(w)]
        assert actions.count("course.delete") == 2 and actions[-1] == "material.delete"
        assert (await audit(w))[-1][2] == {"courses": 2}
        async with client(w) as c:
            restored = await c.post(f"/api/materials/{SYN}/restore", json={})
        assert restored.status_code == 200, restored.text
        assert set(restored.json()["restored_courses"]) == {str(active), str(archived)}
        rows = await w.rows("SELECT id, deleted_at, deleted_by_material, archived_at, position FROM courses")
        after = {r[0]: r[1:] for r in rows}
        assert after[active][0] is None and after[active][1] is None and after[active][3] == 1
        assert after[archived][0] is None and after[archived][2] is not None and after[archived][3] is None
        assert (await audit(w))[-1] == ("material.restore", SYN, {"courses": 2})


async def test_cascade_is_refused_whole_by_an_open_run(pg: Pg, tmp_path: Path) -> None:
    lessons_manage()
    async with world(pg, tmp_path) as w:
        await course_columns(w)
        await imported(w)
        await add_course(w, "First course")
        second = await add_course(w, "Second course")
        lesson = await w.scalar("SELECT id FROM lessons WHERE course_id = :c", c=second)
        await execute(
            w,
            "INSERT INTO compile_runs (owner_id, lesson_id, cap_usd, task_budgets) VALUES (:o, :l, 1, '{}')",
            o=w.users["ivan"],
            l=lesson,
        )
        async with client(w) as c:
            response = await delete(c, SYN, {"confirm": SYN, "cascade": True})
        assert (response.status_code, response.json()) == (409, {"error": "compile_running"})
        assert (await material_state(w))[0] is None
        assert await w.scalar("SELECT count(*) FROM courses WHERE deleted_at IS NOT NULL") == 0
        assert await audit(w) == []


async def test_course_deleted_on_its_own_stays_deleted_after_the_material_restore(
    pg: Pg, tmp_path: Path
) -> None:
    lessons_manage()
    async with world(pg, tmp_path) as w:
        await course_columns(w)
        await imported(w)
        own = await add_course(w, "Deleted first")
        cascaded = await add_course(w, "Cascaded")
        async with client(w) as c:
            gone = await c.request("DELETE", f"/api/courses/{own}", json={"confirm": "Deleted first"})
            assert gone.status_code == 200, gone.text
            assert (await delete(c, SYN, {"confirm": SYN, "cascade": True})).status_code == 200
            own_restore = await c.post(f"/api/courses/{cascaded}/restore", json={})
            restored = await c.post(f"/api/materials/{SYN}/restore", json={})
            later = await c.post(f"/api/courses/{own}/restore", json={})
        assert (own_restore.status_code, own_restore.json()) == (409, {"error": "material_deleted"})
        assert restored.json()["restored_courses"] == [str(cascaded)]
        assert later.status_code == 200
        assert await w.scalar("SELECT count(*) FROM courses WHERE deleted_at IS NOT NULL") == 0
