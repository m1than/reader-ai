"""Receive, status, requeue, the import job, the tables and the routes against Postgres (SRV-INGEST [db]).

@trace FR-INGEST-11 FR-INGEST-12 FR-INGEST-13 FR-INGEST-14 FR-INGEST-15 FR-INGEST-17
@trace INV-INGEST-2 INV-INGEST-3 INV-INGEST-5 INV-INGEST-6 INV-INGEST-7 VEC-INGEST-5
"""

from __future__ import annotations

import io
import json
import logging
import time
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from .conftest import (
    CANARY,
    FIXTURES,
    MATERIAL_SHA,
    Pg,
    World,
    build_artifact,
    client,
    need,
    tar_of,
    world,
)

pytestmark = pytest.mark.anyio


def tar(**manifest: Any) -> io.BytesIO:
    spans = [json.loads(x) for x in build_artifact()["spans.jsonl"].splitlines()]
    spans[1]["text"] = f"Абзац {CANARY}"
    return io.BytesIO(tar_of(build_artifact(spans=spans, **manifest)))


async def receive(w: World, stream: io.BytesIO, **kw: Any) -> dict[str, Any]:
    mod = need("reader.ingest.receive")
    result: dict[str, Any] = await mod.receive(
        stream, config=w.config, engine=w.engine, jobs=kw.pop("jobs", w.jobs), blobs_root=w.blobs, **kw
    )
    return result


async def refused(w: World, stream: io.BytesIO, **kw: Any) -> str:
    ingest_errors = need("reader.ingest.errors")
    with pytest.raises(ingest_errors.IngestError) as failure:
        await receive(w, stream, **kw)
    return str(failure.value.code)


def stored(w: World) -> list[str]:
    root = w.blobs / "ingest"
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


async def key_rows(w: World) -> int:
    return int(await w.scalar("SELECT count(*) FROM job_keys"))


# ---- FR-INGEST-11, -13, -17, INV-INGEST-7: receive, import, span ids ----


async def test_receive_then_import(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        result = await receive(w, tar())
        assert result["result"] == "queued"
        assert stored(w) == [result["artifact_id"]]
        await w.work()
        owner = w.users["ivan"]
        rows = await w.rows("SELECT owner_id, sha256, config_id, page_count FROM materials")
        assert [tuple(r) for r in rows] == [(owner, MATERIAL_SHA, "synthetic", 2)]
        assert await w.scalar("SELECT count(*) FROM material_spans WHERE owner_id = :o", o=owner) == 8
        assert await w.scalar("SELECT count(*) FROM material_outline WHERE owner_id = :o", o=owner) == 2
        version = await w.scalar("SELECT converter_version FROM ingests")
        assert version == "docling-2.151.1+r1"
        again = await receive(w, tar())
        assert again["result"] == "already_done"
        await w.work()
        assert await w.scalar("SELECT count(*) FROM ingests") == 1
        assert await w.scalar("SELECT count(*) FROM material_spans") == 8


async def test_unknown_material_and_version_mismatch(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        assert await refused(w, tar(material_sha256="c" * 64)) == "unknown_material"
        assert await refused(w, tar(converter_version="docling-9+r9")) == "version_mismatch"
        assert stored(w) == [] and await key_rows(w) == 0


async def test_frozen_version_is_refused_at_receive_and_import(
    pg: Pg, tmp_path: Path, monkeypatch: Any
) -> None:
    ingest = need("reader.ingest")
    async with world(pg, tmp_path) as w:
        await receive(w, tar())
        await w.work()
        monkeypatch.setattr(ingest, "CONVERTER_VERSION", "docling-2.151.1+r2")
        assert await refused(w, tar(converter_version="docling-2.151.1+r2")) == "version_frozen"
        status = need("reader.ingest.status")
        report = await status.status(w.engine, w.jobs, MATERIAL_SHA)
        assert report == {"status": "succeeded", "converter_version": "docling-2.151.1+r1"}
        # A job under another version reaching the import is refused permanently.
        artifact = next(iter(stored(w)))
        await w.jobs.enqueue(
            "ingest.import",
            {
                "material_sha256": MATERIAL_SHA,
                "converter_version": "docling-2.151.1+r2",
                "artifact_id": artifact,
                "material_id": "synthetic",
            },
        )
        await w.work()
        codes = await w.rows("SELECT outcome, error_code FROM job_keys ORDER BY created_at")
        assert tuple(codes[-1]) == ("dead", "version_frozen")
        assert await w.scalar("SELECT count(*) FROM ingests") == 1


async def test_spans_route_returns_stable_span_ids(pg: Pg, tmp_path: Path) -> None:
    artifact = need("reader.ingest.artifact")
    async with world(pg, tmp_path) as w:
        await receive(w, tar())
        await w.work()
        async with client(w) as c:
            first = (await c.get("/api/materials/synthetic/spans", params={"page": 1})).json()
        await receive(w, tar())  # a further receive and worker pass change nothing
        await w.work()
        async with client(w) as c:
            second = (await c.get("/api/materials/synthetic/spans", params={"page": 1})).json()
        ids = [s["span_id"] for s in first["spans"]]
        assert ids == [artifact.span_id(MATERIAL_SHA, s["seq"]) for s in first["spans"]]
        assert len(set(ids)) == len(ids) == 4
        assert first == second


# ---- INV-INGEST-3, FR-INGEST-11: nothing written on rejection; storage kept after it ----


async def test_rejected_receive_changes_nothing(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        files = build_artifact()
        bad = dict(files)
        bad["outline.json"] = b"[]"
        assert await refused(w, io.BytesIO(tar_of(bad))) == "hash_mismatch"
        assert await refused(w, io.BytesIO(tar_of(files)[:900])) == "tar_format"
        assert await refused(w, tar(pages_converted=1)) == "incomplete"
        assert stored(w) == [] and await key_rows(w) == 0
        assert not any((w.blobs / "ingest").glob(".tmp-*")) if (w.blobs / "ingest").exists() else True


async def test_deadline_before_storage_writes_nothing(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        assert await refused(w, tar(), deadline=time.monotonic() - 1) == "timeout"
        assert stored(w) == [] and await key_rows(w) == 0


class BrokenJobs:
    async def enqueue(self, *_: Any, **__: Any) -> Any:
        raise OSError("database went away")


async def test_enqueue_failure_after_storage_keeps_the_directory(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        assert await refused(w, tar(), jobs=BrokenJobs()) == "enqueue_failed"
        (artifact,) = stored(w)
        artifact_mod = need("reader.ingest.artifact")
        assert artifact_mod.validate_dir(w.blobs / "ingest" / artifact)["span_count"] == 8
        again = await receive(w, tar())
        assert again["result"] == "queued" and stored(w) == [artifact]
        await w.work()
        assert await w.scalar("SELECT count(*) FROM ingests") == 1


async def test_corrupted_stored_directory_is_replaced(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        first = await receive(w, tar())
        target = w.blobs / "ingest" / first["artifact_id"] / "spans.jsonl"
        target.write_bytes(b"corrupt\n")
        await receive(w, tar())
        artifact_mod = need("reader.ingest.artifact")
        assert artifact_mod.validate_dir(target.parent)["span_count"] == 8
        assert stored(w) == [first["artifact_id"]]


# ---- FR-INGEST-12: status and requeue ----


async def test_status_and_requeue(pg: Pg, tmp_path: Path) -> None:
    status = need("reader.ingest.status")
    requeue = need("reader.ingest.requeue")
    async with world(pg, tmp_path) as w:
        assert (await status.status(w.engine, w.jobs, MATERIAL_SHA))["status"] == "none"
        result = await receive(w, tar())
        assert (await status.status(w.engine, w.jobs, MATERIAL_SHA))["status"] == "queued"
        # Make the artifact unreadable: the import goes dead (FileNotFoundError is permanent).
        for path in (w.blobs / "ingest" / result["artifact_id"]).iterdir():
            path.unlink()
        await w.work()
        assert (await status.status(w.engine, w.jobs, MATERIAL_SHA))["status"] == "dead"
        report = await requeue.requeue(w.engine, w.jobs, MATERIAL_SHA)
        assert report["result"] == "queued"


# ---- FR-INGEST-14, INV-INGEST-6: tables ----


async def test_tables_have_owner_ids_and_the_downgrade_drops_only_them(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        tables = ("materials", "ingests", "material_spans", "material_outline")
        for table in tables:
            fk = await w.scalar(
                "SELECT count(*) FROM information_schema.referential_constraints rc"
                " JOIN information_schema.key_column_usage k ON k.constraint_name = rc.constraint_name"
                " WHERE k.table_name = :t AND k.column_name = 'owner_id'",
                t=table,
            )
            nullable = await w.scalar(
                "SELECT is_nullable FROM information_schema.columns"
                " WHERE table_name = :t AND column_name = 'owner_id'",
                t=table,
            )
            assert fk == 1 and nullable == "NO", table
        unique = await w.scalar(
            "SELECT count(*) FROM pg_indexes"
            " WHERE tablename = 'ingests' AND indexdef LIKE '%UNIQUE%(material_id)%'"
        )
        assert unique == 1
        import importlib.util

        path = need("reader.ingest").MIGRATIONS / "ingest_0001.py"
        spec = importlib.util.spec_from_file_location("ingest_0001", path)
        assert spec is not None and spec.loader is not None
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        assert migration.branch_labels == ("ingest",)
        assert all(any(t in s for s in migration.DOWNGRADE) for t in tables)
        assert not any("users" in s or "job_keys" in s for s in migration.DOWNGRADE)


# ---- FR-INGEST-15, VEC-INGEST-5: routes and status map ----


def test_status_map_vector() -> None:
    store = need("reader.ingest.store")
    rows = YAML(typ="safe").load((FIXTURES / "status-map.yaml").read_text())["rows"]
    for row in rows:
        assert store.api_status(row["job_status"], row["ingested"]) == row["status"], row


async def test_routes(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        async with client(w) as c:
            listing = (await c.get("/api/materials")).json()["materials"]
            assert [m["id"] for m in listing] == ["synthetic", "other"]
            assert listing[0]["status"] == "not_ingested" and listing[0]["span_count"] is None
            assert (await c.get("/api/materials/synthetic/outline")).status_code == 404
        await receive(w, tar())
        await w.work()
        async with client(w) as c:
            first = (await c.get("/api/materials")).json()["materials"][0]
            assert first["status"] == "imported" and first["span_count"] == 8 and first["page_count"] == 2
            assert first["pages_without_text"] == [2]
            outline = (await c.get("/api/materials/synthetic/outline")).json()["outline"]
            assert [o["title"] for o in outline] == ["Глава 1", "Раздел"]
            page2 = (await c.get("/api/materials/synthetic/spans", params={"page": 2})).json()["spans"]
            assert [s["kind"] for s in page2] == ["formula", "formula", "table", "figure"]
            assert (await c.get("/api/materials/synthetic/spans", params={"page": 3})).status_code == 400
            assert (await c.get("/api/materials/synthetic/spans", params={"page": "x"})).status_code == 400
            assert (await c.get("/api/materials/nope/outline")).status_code == 404
        async with client(w, "anna") as c:
            response = await c.get("/api/materials")
            assert response.status_code == 403 and response.json() == {"error": "owner_only"}
        async with client(w, None) as c:
            assert (await c.get("/api/materials")).status_code == 401


# ---- INV-INGEST-5 [db]: no material text in logs or error responses ----


async def test_no_material_text_in_worker_logs(pg: Pg, tmp_path: Path, caplog: Any) -> None:
    caplog.set_level(logging.DEBUG)
    async with world(pg, tmp_path) as w:
        await receive(w, tar())
        await w.work()
        async with client(w) as c:
            body = (await c.get("/api/materials/synthetic/spans", params={"page": 9})).text
        assert CANARY not in body
    assert CANARY not in caplog.text


# ---- INV-INGEST-2: one ingest per material under repeated receives ----


async def test_repeated_receives_leave_one_ingest(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        results = [await receive(w, tar()) for _ in range(3)]
        assert [r["result"] for r in results] == ["queued", "already_queued", "already_queued"]
        await w.work()
        await receive(w, tar())
        await w.work()
        assert await w.scalar("SELECT count(*) FROM ingests") == 1
        assert await w.scalar("SELECT count(*) FROM material_spans") == 8


# ---- FR-INGEST-13: the import path never loads the host converter ----


def test_server_side_modules_do_not_import_the_host_converter() -> None:
    import subprocess
    import sys

    code = (
        "import sys\n"
        "import reader.ingest.jobs, reader.ingest.receive, reader.ingest.status, reader.ingest.routes\n"
        "import reader.ingest.requeue\n"
        "bad = [m for m in sys.modules if m.startswith(('reader.ingest.host', 'docling', 'pypdfium2'))]\n"
        "print(bad)\n"
        "sys.exit(1 if bad else 0)\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
