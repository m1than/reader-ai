"""Browser upload, the uploads state machine, the inbox commands and receive under a claim (SRV-INGEST [db]).

@trace FR-INGEST-18 FR-INGEST-19 FR-INGEST-20 FR-INGEST-23 FR-INGEST-24
@trace INV-INGEST-9 INV-INGEST-10 VEC-INGEST-6 VEC-INGEST-7
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import logging
import os
import secrets
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from .conftest import (
    MATERIAL_SHA,
    ORIGIN,
    SESSION_COOKIE,
    Pg,
    World,
    build_artifact,
    need,
    session_for,
    tar_of,
    world,
)

pytestmark = pytest.mark.anyio

HERE = Path(__file__).resolve().parent
CANARY_NAME = "QUOKKAZEBRA-name.pdf"
WATCHER_SHA = "c" * 64


def pdf(tag: bytes = b"", size: int = 2048) -> bytes:
    """A body that passes the magic check (VEC-INGEST-7), unique per tag; never a real document."""
    head = b"%PDF-1.7\n" + tag + b"\n"
    return head + b"x" * max(0, size - len(head) - 6) + b"%%EOF\n"


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def inbox(w: World) -> list[str]:
    root = w.blobs / "inbox"
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


async def upload_rows(w: World) -> int:
    return int(await w.scalar("SELECT count(*) FROM uploads"))


@asynccontextmanager
async def api(w: World, handle: str | None = "ivan", **state: Any) -> AsyncIterator[httpx.AsyncClient]:
    """The api with this world's blobs root, reader.yaml materials and jobs; `state` overrides app.state."""
    from reader.api.app import create_app

    app = create_app(w.settings, w.engine, run_cleanup_task=False, jobs=w.jobs)
    app.state.blobs_root = w.blobs
    app.state.ingest_materials = w.config.materials
    app.state.converter_source_sha256 = WATCHER_SHA
    for key, value in state.items():
        setattr(app.state, key, value)
    cookies = {SESSION_COOKIE: await session_for(w, handle)} if handle else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers={"Origin": ORIGIN}, cookies=cookies
    ) as c:
        yield c


async def post(
    c: httpx.AsyncClient,
    body: Any,
    *,
    name: str = "Book.pdf",
    language: str = "ru",
    content_type: str = "application/pdf",
) -> httpx.Response:
    headers = {"Content-Type": content_type}
    if name is not None:
        headers["X-Reader-Filename"] = name
    return await c.post(f"/api/materials/uploads?language={language}", content=body, headers=headers)


async def uploaded(w: World, body: bytes, **kw: Any) -> dict[str, Any]:
    async with api(w) as c:
        response = await post(c, body, **kw)
    assert response.status_code == 201, response.text
    view: dict[str, Any] = response.json()
    return view


# ---- the inbox commands (FR-INGEST-20) as the host sees them: exit code and stdout bytes ----


async def cmd(w: World, *argv: str) -> tuple[int, bytes]:
    inbox_mod = need("reader.ingest.inbox")
    out = io.BytesIO()
    code = await inbox_mod.execute(list(argv), engine=w.engine, blobs_root=w.blobs, out=out)
    return int(code), out.getvalue()


def line(raw: bytes) -> Any:
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1 and len(raw) <= 4096, raw[:200]
    return json.loads(raw)


CANCEL = b'{"cancel":true}\n'


def host_args(state: str = "ready", busy: str = "none", strays: int = 0) -> list[str]:
    return [
        "--host-state", state, "--strays", str(strays), "--converter-version", "docling-2.151.1+r2",
        "--watcher-sha", WATCHER_SHA, "--busy", busy,
    ]


async def claim(w: World) -> dict[str, Any]:
    code, raw = await cmd(w, "next", "--claim", "--wait", "1", *host_args())
    assert code == 0, raw
    got = line(raw)["upload"]
    assert got is not None
    return dict(got)


async def state_of(w: World, upload_id: str) -> tuple[str, int, str | None]:
    row = (await w.rows("SELECT state, attempts, code FROM uploads WHERE id = :i", i=upload_id))[0]
    return str(row[0]), int(row[1]), row[2]


def artifact_tar(material_sha: str) -> io.BytesIO:
    return io.BytesIO(tar_of(build_artifact(material_sha256=material_sha)))


async def receive_upload(w: World, body: bytes, upload_id: str, claim_token: str) -> dict[str, Any]:
    receive = need("reader.ingest.receive")
    result: dict[str, Any] = await receive.receive(
        artifact_tar(sha(body)),
        config=w.config,
        engine=w.engine,
        jobs=w.jobs,
        blobs_root=w.blobs,
        upload_id=upload_id,
        claim=claim_token,
    )
    return result


async def to_sending(w: World, body: bytes) -> tuple[dict[str, Any], dict[str, Any]]:
    view = await uploaded(w, body)
    got = await claim(w)
    code, raw = await cmd(w, "progress", "--upload-id", got["id"], "--claim", got["claim"], "--phase", "sending")
    assert (code, raw) == (0, b'{"ok":true}\n')
    return view, got


async def lease_passed(w: World, upload_id: str, seconds: int = 121) -> None:
    """The last renewal was `seconds` ago (lease 120 s): rewrite lease_until instead of sleeping."""
    async with w.engine.begin() as conn:
        from sqlalchemy import text

        await conn.execute(
            text("UPDATE uploads SET lease_until = now() + make_interval(secs => :s) WHERE id = :i"),
            {"s": 120 - seconds, "i": upload_id},
        )


@contextlib.contextmanager
def small_caps(monkeypatch: pytest.MonkeyPatch, **caps: int) -> Iterator[Any]:
    params = need("reader.ingest.params")
    for name, value in caps.items():
        monkeypatch.setattr(params, name, value)
    yield params


# ---- FR-INGEST-18: the upload route ----


async def test_upload_stores_the_body_and_queues_it(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        body = pdf(b"one")
        view = await uploaded(w, body, name="%D0%9A%D0%BD%D0%B8%D0%B3%D0%B0.pdf", language="other")
        digest = sha(body)
        assert inbox(w) == [f"{digest}.pdf"]
        path = w.blobs / "inbox" / f"{digest}.pdf"
        assert path.read_bytes() == body
        assert stat.S_IMODE(path.stat().st_mode) == 0o440
        assert stat.S_ISDIR((w.blobs / "inbox").lstat().st_mode)
        assert set(view) == {
            "id", "material_id", "filename", "size", "language", "state", "code", "pages_done", "pages_total",
            "attempts", "created_at", "retry",
        }
        assert view["material_id"] == f"up-{digest[:12]}"
        assert (view["filename"], view["size"], view["language"]) == ("Книга.pdf", len(body), "other")
        assert (view["state"], view["code"], view["attempts"], view["retry"]) == ("queued", None, 0, None)
        row = (await w.rows("SELECT owner_id, sha256, state FROM uploads"))[0]
        assert tuple(row) == (w.users["ivan"], digest, "queued")


async def test_upload_refusals(pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async with world(pg, tmp_path) as w:
        with small_caps(monkeypatch, UPLOAD_BYTES_MAX=4096):
            cases: list[tuple[dict[str, Any], int, str]] = [
                ({"body": pdf(), "language": "de"}, 400, "language"),
                ({"body": pdf(), "name": "%GG"}, 400, "filename"),
                ({"body": pdf(), "name": None}, 400, "filename"),
                ({"body": b""}, 400, "empty"),
                ({"body": b"PK\x03\x04" + b"x" * 100}, 415, "not_pdf"),
                ({"body": pdf(size=4097)}, 413, "too_large"),
                ({"body": pdf(), "content_type": "application/pdf; charset=binary"}, 201, ""),
            ]
            async with api(w) as c:
                for kw, status, code in cases:
                    response = await post(c, **kw)
                    assert response.status_code == status, (kw.get("name"), response.text)
                    if code:
                        assert response.json() == {"error": code}
                assert (await post(c, pdf(size=4096), name="cap.pdf")).status_code == 201  # exactly the cap
            async with api(w, handle="anna") as c:
                response = await post(c, pdf(b"anna"))
                assert (response.status_code, response.json()) == (403, {"error": "owner_only"})
        assert await upload_rows(w) == 2
        assert not [n for n in inbox(w) if n.startswith(".tmp-")]


async def test_upload_lock_is_instance_wide_and_released(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        params = need("reader.ingest.params")
        from sqlalchemy import text

        async with w.engine.connect() as holder:
            assert (await holder.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": params.UPLOAD_LOCK_KEY})).scalar()
            async with api(w) as c:
                response = await post(c, pdf())
            assert (response.status_code, response.json()) == (409, {"error": "upload_busy"})
            await holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": params.UPLOAD_LOCK_KEY})
        async with api(w) as c:
            assert (await post(c, pdf(b"a"))).status_code == 201
            assert (await post(c, b"junk")).status_code == 415
        async with w.engine.connect() as probe:
            got = (await probe.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": params.UPLOAD_LOCK_KEY})).scalar()
            assert got is True  # every path released it
            await probe.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": params.UPLOAD_LOCK_KEY})


async def test_upload_dedupe_runs_before_the_quota(pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async with world(pg, tmp_path) as w:
        with small_caps(monkeypatch, OPEN_MAX=2):
            first, second = pdf(b"1"), pdf(b"2")
            one = await uploaded(w, first)
            await uploaded(w, second)
            async with api(w) as c:
                again = await post(c, first)
                assert (again.status_code, again.json()["id"]) == (200, one["id"])
                full = await post(c, pdf(b"3"))
                assert (full.status_code, full.json()) == (409, {"error": "inbox_full"})
        assert await upload_rows(w) == 2
        assert sorted(inbox(w)) == sorted([f"{sha(first)}.pdf", f"{sha(second)}.pdf"])


async def test_upload_of_a_declared_material(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        body = pdf(b"declared")
        from reader.config.model import Material

        declared = Material(id="declared", file="./d.pdf", sha256=sha(body), license="personal-use", language="ru")
        async with api(w, ingest_materials=(*w.config.materials, declared)) as c:
            response = await post(c, body)
        assert (response.status_code, response.json()) == (200, {"material_id": "declared", "upload": None})
        assert await upload_rows(w) == 0 and inbox(w) == []


async def test_upload_quota_by_bytes_is_exact(pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async with world(pg, tmp_path) as w:
        with small_caps(monkeypatch, BYTES_MAX=5000):
            await uploaded(w, pdf(b"a", size=2500))
            await uploaded(w, pdf(b"b", size=2500))  # 5,000 = the bound: accepted
            async with api(w) as c:
                response = await post(c, pdf(b"c", size=100))
            assert (response.status_code, response.json()) == (409, {"error": "inbox_full"})
            # failed uploads count; received ones (handed to the import) do not
            async with w.engine.begin() as conn:
                from sqlalchemy import text

                await conn.execute(text("UPDATE uploads SET state = 'received' WHERE size = 2500"))
            async with api(w) as c:
                assert (await post(c, pdf(b"c", size=100))).status_code == 201
                listed = (await c.get("/api/materials")).json()
            assert listed["inbox_retained_bytes"] == 5000


async def test_upload_id_collision(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        body = pdf(b"collide")
        digest = sha(body)
        other_sha = digest[:12] + ("0" if digest[12] != "0" else "1") + digest[13:]
        async with w.engine.begin() as conn:
            from sqlalchemy import text

            await conn.execute(
                text(
                    "INSERT INTO uploads (id, owner_id, sha256, size, filename, language, state)"
                    " VALUES (gen_random_uuid(), :o, :s, 10, 'x.pdf', 'ru', 'failed')"
                ),
                {"o": w.users["ivan"], "s": other_sha},
            )
        async with api(w) as c:
            response = await post(c, body)
        assert (response.status_code, response.json()) == (409, {"error": "id_collision"})
        from reader.config.model import Material

        body2 = pdf(b"collide-2")
        clash = Material.model_construct(  # a declared up- id (SRV-CONFIG refuses it at load; injected here)
            id=f"up-{sha(body2)[:12]}", file="./x.pdf", sha256="d" * 64, license="personal-use", language="ru"
        )
        async with api(w, ingest_materials=(clash,)) as c:
            response = await post(c, body2)
        assert (response.status_code, response.json()) == (409, {"error": "id_collision"})
        assert await upload_rows(w) == 1 and inbox(w) == []


async def test_upload_config_unavailable_reads_no_byte(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        os.environ["READER_CONFIG"] = str(tmp_path / "missing.yaml")
        async with api(w, ingest_materials=None) as c:
            response = await post(c, pdf())
        assert (response.status_code, response.json()) == (503, {"error": "config_unavailable"})
        assert not [n for n in inbox(w) if n.startswith(".tmp-")] and await upload_rows(w) == 0


async def test_upload_inbox_that_is_a_symlink_is_refused(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (w.blobs / "inbox").symlink_to(elsewhere)
        async with api(w) as c:
            response = await post(c, pdf())
        assert (response.status_code, response.json()) == (500, {"error": "inbox_invalid"})
        assert list(elsewhere.iterdir()) == []


async def test_upload_body_deadline(pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async with world(pg, tmp_path) as w:
        with small_caps(monkeypatch, BODY_DEADLINE_S=1):

            async def slow() -> AsyncIterator[bytes]:
                yield b"%PDF-1.7\n"
                await asyncio.sleep(2.5)
                yield b"%%EOF\n"

            async with api(w) as c:
                response = await post(c, slow())
        assert (response.status_code, response.json()) == (408, {"error": "body_timeout"})
        assert inbox(w) == [] and await upload_rows(w) == 0


async def test_upload_sweeps_crash_leftovers(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        kept = await uploaded(w, pdf(b"kept"))
        root = w.blobs / "inbox"
        (root / ".tmp-0123456789abcdef0123456789abcdef").write_bytes(b"partial")
        orphan = root / f"{'e' * 64}.pdf"
        orphan.write_bytes(pdf(b"orphan"))
        (root / "notes.txt").write_bytes(b"left alone")
        await uploaded(w, pdf(b"next"))
        names = inbox(w)
        assert ".tmp-0123456789abcdef0123456789abcdef" not in names and orphan.name not in names
        assert "notes.txt" in names
        assert f"{(await w.scalar('SELECT sha256 FROM uploads WHERE id = :i', i=kept['id']))}.pdf" in names


async def test_upload_canary_filename_is_never_logged(pg: Pg, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """FR-INGEST-24: the filename appears in no log record of the api or the inbox commands."""
    caplog.set_level(logging.DEBUG)
    async with world(pg, tmp_path) as w:
        body = pdf(b"canary")
        view = await uploaded(w, body, name=CANARY_NAME)
        async with api(w) as c:
            await post(c, b"not a pdf", name=CANARY_NAME)
            await c.get(f"/api/materials/uploads/{view['id']}")
            await c.get("/api/materials")
        got = await claim(w)
        await cmd(w, "fail", "--upload-id", got["id"], "--claim", got["claim"], "--code", "convert_failed")
    assert "QUOKKAZEBRA" not in caplog.text
    assert view["filename"] == CANARY_NAME


# ---- INV-INGEST-9: a refused upload leaves nothing; leftovers of a killed api are swept ----


@settings(max_examples=12, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.binary(max_size=64).filter(lambda b: not b.startswith(b"%PDF-")))
async def test_refused_bodies_leave_nothing(pg: Pg, tmp_path_factory: pytest.TempPathFactory, junk: bytes) -> None:
    async with world(pg, tmp_path_factory.mktemp("w")) as w:
        async with api(w) as c:
            response = await post(c, junk)
        assert response.status_code in (400, 415)
        assert inbox(w) == [] and await upload_rows(w) == 0  # the inbox may exist, empty


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_killed_api_leftovers_are_swept_by_the_next_upload(pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async with world(pg, tmp_path) as w:
        port = _free_port()
        env = {
            **os.environ,
            "READER_TEST_PG_HOST": pg.host,
            "READER_TEST_PG_PORT": str(pg.port),
            "READER_TEST_PG_USER": pg.user,
            "READER_TEST_PG_DB": pg.database,
            "READER_TEST_PG_PASSWORD": pg.password,
        }
        server = subprocess.Popen(  # noqa: S603
            [sys.executable, "-I", str(HERE / "upload_server.py"), str(tmp_path / "reader.yaml"), str(w.blobs), str(port)],
            env=env,
        )
        try:
            for _ in range(100):
                with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
                time.sleep(0.1)
            cookie = await session_for(w, "ivan")
            sock = socket.create_connection(("127.0.0.1", port))
            head = (
                f"POST /api/materials/uploads?language=ru HTTP/1.1\r\nHost: localhost\r\nOrigin: {ORIGIN}\r\n"
                f"Cookie: {SESSION_COOKIE}={cookie}\r\nContent-Type: application/pdf\r\nX-Reader-Filename: k.pdf\r\n"
                "Content-Length: 1000000\r\n\r\n"
            )
            sock.sendall(head.encode() + b"%PDF-1.7\n" + b"x" * 300_000)
            for _ in range(100):
                if any(n.startswith(".tmp-") for n in inbox(w)):
                    break
                time.sleep(0.05)
            assert any(n.startswith(".tmp-") for n in inbox(w))
            server.send_signal(signal.SIGKILL)
            server.wait(10)
            sock.close()
        finally:
            if server.poll() is None:
                server.kill()
        # a second crash, simulated: after the rename and before the insert
        orphan = w.blobs / "inbox" / f"{sha(pdf(b'orphan'))}.pdf"
        orphan.write_bytes(pdf(b"orphan"))
        with small_caps(monkeypatch, OPEN_MAX=1):
            await uploaded(w, pdf(b"after"))  # the quota sees neither leftover
        assert inbox(w) == [f"{sha(pdf(b'after'))}.pdf"]


async def test_cancel_then_reupload(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        body = pdf(b"again")
        first = await uploaded(w, body)
        async with api(w) as c:
            assert (await c.post(f"/api/materials/uploads/{first['id']}/cancel")).status_code == 200
            assert (await c.get(f"/api/materials/uploads/{first['id']}")).status_code == 404
        assert await upload_rows(w) == 0 and inbox(w) == []
        second = await uploaded(w, body)
        assert second["id"] != first["id"] and second["state"] == "queued"
        old = await claim(w)
        code, _ = await cmd(w, "progress", "--upload-id", old["id"], "--claim", old["claim"], "--pages-done", "1", "--pages-total", "2")
        assert code == 0 and (await state_of(w, old["id"]))[0] == "converting"
        async with api(w) as c:
            assert (await c.post(f"/api/materials/uploads/{old['id']}/cancel")).status_code == 200
        third = await uploaded(w, body)
        fresh = await claim(w)
        assert fresh["id"] == third["id"] and fresh["claim"] != old["claim"]
        for argv in (
            ("progress", "--upload-id", old["id"], "--claim", old["claim"], "--phase", "convert"),
            ("fetch", "--upload-id", old["id"], "--claim", old["claim"]),
        ):
            assert await cmd(w, *argv) == (3, CANCEL)
        errors = need("reader.ingest.errors")
        with pytest.raises(errors.Cancel):
            await receive_upload(w, body, old["id"], old["claim"])


async def test_discard_then_reupload_and_refusals_once_the_key_exists(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        body = pdf(b"discard")
        view = await uploaded(w, body)
        got = await claim(w)
        assert (await cmd(w, "fail", "--upload-id", got["id"], "--claim", got["claim"], "--code", "convert_failed"))[0] == 0
        async with api(w) as c:
            failed = (await c.get(f"/api/materials/uploads/{view['id']}")).json()
            assert (failed["state"], failed["code"], failed["retry"]) == ("failed", "convert_failed", "reconvert")
            assert (await c.post(f"/api/materials/uploads/{view['id']}/cancel")).status_code == 409
            assert (await c.delete(f"/api/materials/uploads/{view['id']}")).status_code == 200
        assert await upload_rows(w) == 0 and inbox(w) == []
        _, sending = await to_sending(w, body)
        assert (await receive_upload(w, body, sending["id"], sending["claim"]))["result"] == "queued"
        async with api(w) as c:
            for request in (c.post(f"/api/materials/uploads/{sending['id']}/cancel"), c.delete(f"/api/materials/uploads/{sending['id']}")):
                response = await request
                assert (response.status_code, response.json()) == (409, {"error": "state"})
        assert await upload_rows(w) == 1


# ---- FR-INGEST-19: transitions, lease expiry, key authority, Retry ----


async def test_lease_renew_or_cancel(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        await uploaded(w, pdf(b"lease"))
        got = await claim(w)
        args = ("progress", "--upload-id", got["id"], "--claim", got["claim"], "--phase", "convert")
        await lease_passed(w, got["id"], 119)
        assert await cmd(w, *args) == (0, b'{"ok":true}\n')  # renewed
        await lease_passed(w, got["id"], 121)
        assert await cmd(w, *args) == (3, CANCEL)  # never resurrected
        assert (await state_of(w, got["id"]))[:2] == ("queued", 1)


async def test_expiry_three_times_is_host_lost_and_api_reads_apply_it(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        view = await uploaded(w, pdf(b"lost"))
        for attempt in range(3):
            got = await claim(w)
            await lease_passed(w, got["id"])
            async with api(w) as c:  # no watcher: the api's read applies the expiry
                seen = (await c.get(f"/api/materials/uploads/{view['id']}")).json()
            if attempt < 2:
                assert (seen["state"], seen["attempts"]) == ("queued", attempt + 1)
        assert (seen["state"], seen["code"], seen["retry"]) == ("failed", "host_lost", "reconvert")
        async with api(w) as c:
            assert (await c.post(f"/api/materials/uploads/{view['id']}/retry")).status_code == 200
        assert await state_of(w, view["id"]) == ("queued", 0, None)


async def test_progress_moves_state_and_counts(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        view = await uploaded(w, pdf(b"progress"))
        got = await claim(w)
        assert (await state_of(w, got["id"]))[0] == "claimed"
        base = ("progress", "--upload-id", got["id"], "--claim", got["claim"])
        assert (await cmd(w, *base, "--pages-done", "50", "--pages-total", "120"))[0] == 0
        async with api(w) as c:
            seen = (await c.get(f"/api/materials/uploads/{view['id']}")).json()
            converter = (await c.get("/api/materials/converter")).json()
        assert (seen["state"], seen["pages_done"], seen["pages_total"]) == ("converting", 50, 120)
        assert converter["busy"] == "claim" and converter["state"] == "online"
        assert (await cmd(w, *base, "--pages-done", "121", "--pages-total", "120"))[0] == 1
        assert (await cmd(w, *base, "--phase", "sending"))[0] == 0
        assert (await state_of(w, got["id"]))[0] == "sending"


async def test_retry_replies_for_a_dead_import_or_embed(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        body = pdf(b"dead")
        view, got = await to_sending(w, body)
        result = await receive_upload(w, body, got["id"], got["claim"])
        stored = w.blobs / "ingest" / result["artifact_id"]
        aside = tmp_path / "aside"
        stored.rename(aside)
        await w.work()  # artifact missing: permanent, the import key goes dead
        async with api(w) as c:
            seen = (await c.get(f"/api/materials/uploads/{view['id']}")).json()
            assert (seen["state"], seen["code"], seen["retry"]) == ("failed", "artifact_missing", "requeue")
            assert (await c.delete(f"/api/materials/uploads/{view['id']}")).status_code == 409
            aside.rename(stored)
            response = await c.post(f"/api/materials/uploads/{view['id']}/retry")
            assert (response.status_code, response.json()) == (202, {"retry": "requeued"})
        await w.work()
        from sqlalchemy import text

        async with w.engine.begin() as conn:  # the embed job is never run here: let it die, then succeed
            await conn.execute(
                text("UPDATE job_keys SET outcome = 'dead', error_code = 'EmbedUnavailable' WHERE kind = 'search.embed'")
            )
        async with api(w) as c:
            seen = (await c.get(f"/api/materials/uploads/{view['id']}")).json()
            assert (seen["state"], seen["code"], seen["retry"]) == ("failed", "EmbedUnavailable", "requeue")
            response = await c.post(f"/api/materials/uploads/{view['id']}/retry")
            assert (response.status_code, response.json()) == (202, {"retry": "requeued"})
        async with w.engine.begin() as conn:
            await conn.execute(text("UPDATE job_keys SET outcome = 'succeeded' WHERE kind = 'search.embed'"))
            # a dead embed key of an ingest that never existed does not make the view failed
            await conn.execute(
                text(
                    "INSERT INTO job_keys (kind, key, args, attempts, outcome, error_code)"
                    " VALUES ('search.embed', 'search.embed:stale', '{}', 3, 'dead', 'IngestNotReady')"
                )
            )
        async with api(w) as c:
            seen = (await c.get(f"/api/materials/uploads/{view['id']}")).json()
        assert (seen["state"], seen["retry"]) == ("ready", None)


class FakeJobs:
    def __init__(self, kind: str) -> None:
        self.kind = kind

    async def requeue_dead(self, kind_name: str, key: str) -> Any:
        jobs_model = need("reader.jobs.model")
        return jobs_model.Result(self.kind)


@pytest.mark.parametrize(
    ("kind", "status", "body"),
    [
        ("queued", 202, {"retry": "requeued"}),
        ("already_queued", 202, {"retry": "already_queued"}),
        ("running", 409, {"error": "busy"}),
        ("unknown_key", 409, {"error": "state"}),
        ("already_done", 200, None),
    ],
)
async def test_retry_reply_per_requeue_result(pg: Pg, tmp_path: Path, kind: str, status: int, body: Any) -> None:
    async with world(pg, tmp_path) as w:
        upload = pdf(b"reply")
        view, got = await to_sending(w, upload)
        await receive_upload(w, upload, got["id"], got["claim"])
        from sqlalchemy import text

        async with w.engine.begin() as conn:
            await conn.execute(text("UPDATE job_keys SET outcome = 'dead', error_code = 'x' WHERE kind = 'ingest.import'"))
        async with api(w, jobs=FakeJobs(kind)) as c:
            response = await c.post(f"/api/materials/uploads/{view['id']}/retry")
        assert response.status_code == status
        if body is not None:
            assert response.json() == body
        else:
            assert response.json()["id"] == view["id"]


async def test_received_without_import_key_is_reconverted(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        body = pdf(b"operator")
        view, got = await to_sending(w, body)
        await receive_upload(w, body, got["id"], got["claim"])
        from sqlalchemy import text

        async with w.engine.begin() as conn:  # an operator removed the key
            await conn.execute(text("DELETE FROM procrastinate_jobs"))
            await conn.execute(text("DELETE FROM job_keys"))
        async with api(w) as c:
            seen = (await c.get(f"/api/materials/uploads/{view['id']}")).json()
            assert (seen["state"], seen["retry"]) == ("failed", "reconvert")
            assert (await c.post(f"/api/materials/uploads/{view['id']}/retry")).status_code == 200
        assert (await state_of(w, view["id"]))[:2] == ("queued", 0)


# ---- FR-INGEST-20: the inbox commands' framing and arguments ----


async def test_next_peek_and_claim(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        assert await cmd(w, "next", "--peek", "--wait", "1", *host_args()) == (0, b'{"queued":0}\n')
        first = await uploaded(w, pdf(b"first"))
        await uploaded(w, pdf(b"second"))
        assert line((await cmd(w, "next", "--peek", "--wait", "1", *host_args()))[1]) == {"queued": 2}
        assert await upload_rows(w) == 2 and (await state_of(w, first["id"]))[0] == "queued"  # peek claimed nothing
        for refused in (host_args(state="setup_missing"), host_args(busy="cli_running"), host_args(busy="claim")):
            code, raw = await cmd(w, "next", "--claim", "--wait", "1", *refused)
            assert code == 0 and line(raw) == {"upload": None}
        got = await claim(w)
        assert set(got) == {"id", "sha256", "size", "claim"} and got["id"] == first["id"]  # the oldest
        assert len(got["claim"]) == 32 and int(got["claim"], 16) >= 0
        await claim(w)
        assert await cmd(w, "next", "--claim", "--wait", "1", *host_args()) == (0, b'{"upload":null}\n')
        host = (await w.rows("SELECT state, strays, converter_version, watcher_sha, busy FROM ingest_hosts"))[0]
        assert tuple(host) == ("ready", 0, "docling-2.151.1+r2", WATCHER_SHA, "none")


async def test_next_peek_waits_for_a_notify(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        started = time.monotonic()
        waiting = asyncio.create_task(cmd(w, "next", "--peek", "--wait", "5", *host_args()))
        await asyncio.sleep(0.5)
        await uploaded(w, pdf(b"notify"))
        code, raw = await waiting
        assert (code, line(raw)) == (0, {"queued": 1})
        assert time.monotonic() - started < 4


async def test_fetch_copies_exactly_the_bytes(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        body = pdf(b"fetch", size=3 * 1024 * 1024 + 7)
        await uploaded(w, body)
        got = await claim(w)
        assert await cmd(w, "fetch", "--upload-id", got["id"], "--claim", got["claim"]) == (0, body)
        assert await cmd(w, "fetch", "--upload-id", got["id"], "--claim", "0" * 32) == (3, CANCEL)
        (w.blobs / "inbox" / f"{got['sha256']}.pdf").unlink()
        assert await cmd(w, "fetch", "--upload-id", got["id"], "--claim", got["claim"]) == (1, b'{"error":"missing"}\n')


async def test_fetch_refuses_a_size_that_differs_from_the_row(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        body = pdf(b"size")
        await uploaded(w, body)
        got = await claim(w)
        path = w.blobs / "inbox" / f"{got['sha256']}.pdf"
        path.chmod(0o640)
        path.write_bytes(body + b"more")
        assert await cmd(w, "fetch", "--upload-id", got["id"], "--claim", got["claim"]) == (1, b'{"error":"missing"}\n')


async def test_fail_codes_and_argument_checks(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        await uploaded(w, pdf(b"fail"))
        got = await claim(w)
        bad = [
            ("fail", "--upload-id", "not-a-uuid", "--claim", got["claim"], "--code", "convert_failed"),
            ("fail", "--upload-id", got["id"], "--claim", "ZZ" * 16, "--code", "convert_failed"),
            ("fail", "--upload-id", got["id"], "--claim", got["claim"], "--code", "made_up"),
            ("next", "--claim", "--wait", "26", *host_args()),
            ("next", "--peek", "--wait", "1", *host_args(strays=10_001)),
            ("progress", "--upload-id", got["id"], "--claim", got["claim"], "--phase", "nap"),
            ("unknown",),
        ]
        for argv in bad:
            code, raw = await cmd(w, *argv)
            assert code == 1 and "error" in line(raw), argv
        assert (await state_of(w, got["id"]))[0] == "claimed"
        for code_name in ("fetch_mismatch", "not_pdf", "inbox_missing", "memory_limit", "work_limit", "timeout", "receive_failed", "bad_reply"):
            assert code_name in need("reader.ingest.inbox").FAIL_CODES
        assert await cmd(w, "fail", "--upload-id", got["id"], "--claim", got["claim"], "--code", "memory_limit") == (0, b'{"ok":true}\n')
        assert await state_of(w, got["id"]) == ("failed", 0, "memory_limit")
        assert await cmd(w, "fail", "--upload-id", str(uuid.uuid4()), "--claim", got["claim"], "--code", "timeout") == (3, CANCEL)


async def test_arguments_are_checked_before_any_database_access(tmp_path: Path) -> None:
    inbox_mod = need("reader.ingest.inbox")
    out = io.BytesIO()
    code = await inbox_mod.execute(
        ["fetch", "--upload-id", "../../etc/passwd", "--claim", "a" * 32], engine=None, blobs_root=tmp_path, out=out
    )
    assert code == 1 and json.loads(out.getvalue()) == {"error": "arguments"}


# ---- FR-INGEST-23: receive under a claim, the import of an uploaded material, the routes ----


async def test_receive_under_a_claim_imports_the_upload(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        body = pdf(b"import")
        view, got = await to_sending(w, body)
        result = await receive_upload(w, body, got["id"], got["claim"])
        assert result["result"] == "queued"
        assert (await state_of(w, got["id"]))[0] == "received"
        await w.work()
        material_id = f"up-{sha(body)[:12]}"
        row = (await w.rows("SELECT config_id, sha256 FROM materials"))[0]
        assert tuple(row) == (material_id, sha(body))
        async with api(w) as c:
            listed = (await c.get("/api/materials")).json()
            seen = (await c.get(f"/api/materials/uploads/{view['id']}")).json()
            outline = await c.get(f"/api/materials/{material_id}/outline")
            spans = await c.get(f"/api/materials/{material_id}/spans?page=1")
        assert seen["state"] == "indexing"
        assert [m["source"] for m in listed["materials"]] == ["config", "config", "upload"]
        mine = listed["materials"][-1]
        assert (mine["id"], mine["status"], mine["language"], mine["license"]) == (material_id, "imported", "ru", "personal-use")
        assert mine["upload"]["id"] == view["id"] and mine["title"] == "Book"
        assert outline.status_code == 200 and spans.status_code == 200
        assert spans.json()["spans"][0]["span_id"].startswith(sha(body) + ":")


async def test_receive_refusals_under_claims(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        errors = need("reader.ingest.errors")
        body, other = pdf(b"mine"), pdf(b"other")
        _, got = await to_sending(w, body)
        _, theirs = await to_sending(w, other)
        for upload_id, token, material in (
            (got["id"], "0" * 32, body),  # a stale claim
            (got["id"], theirs["claim"], body),  # another upload's claim
            (theirs["id"], theirs["claim"], body),  # sha256 differs from the upload's
            (str(uuid.uuid4()), got["claim"], body),  # a deleted upload
        ):
            with pytest.raises(errors.Cancel):
                await receive_upload(w, material, upload_id, token)
        assert not (w.blobs / "ingest").exists() or [p for p in (w.blobs / "ingest").iterdir() if not p.name.startswith(".")] == []
        assert int(await w.scalar("SELECT count(*) FROM job_keys")) == 0
        # a declared material's artifact under an upload's claim is refused too
        with pytest.raises((errors.Cancel, errors.IngestError)):
            receive = need("reader.ingest.receive")
            await receive.receive(
                artifact_tar(MATERIAL_SHA), config=w.config, engine=w.engine, jobs=w.jobs, blobs_root=w.blobs,
                upload_id=got["id"], claim=got["claim"],
            )


async def test_crash_between_enqueue_and_mark_recovers(pg: Pg, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    async with world(pg, tmp_path) as w:
        body = pdf(b"crash")
        view, got = await to_sending(w, body)
        receive = need("reader.ingest.receive")
        with pytest.raises(RuntimeError):
            await receive.receive(
                artifact_tar(sha(body)), config=w.config, engine=w.engine, jobs=w.jobs, blobs_root=w.blobs,
                upload_id=got["id"], claim=got["claim"], before_mark=_crash,
            )
        assert (await state_of(w, got["id"]))[0] == "sending"  # the key exists, the row is not marked
        async with api(w) as c:
            seen = (await c.get(f"/api/materials/uploads/{view['id']}")).json()
            assert seen["state"] == "importing"
            assert (await c.post(f"/api/materials/uploads/{view['id']}/cancel")).status_code == 409
        assert (await state_of(w, got["id"]))[0] == "received"
        # a receive repeated under the same claim passes step 1 on the key and repeats 2-4 harmlessly
        caplog.set_level(logging.INFO)
        again = await receive_upload(w, body, got["id"], got["claim"])
        assert again["result"] in ("already_queued", "queued")
        assert "mark_noop" in caplog.text


def _crash() -> None:
    raise RuntimeError("crash before mark")


async def test_import_of_a_missing_upload_is_dead(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        body = pdf(b"vanish")
        view, got = await to_sending(w, body)
        await receive_upload(w, body, got["id"], got["claim"])
        from sqlalchemy import text

        async with w.engine.begin() as conn:
            await conn.execute(text("DELETE FROM uploads"))
        await w.work()
        assert await w.scalar("SELECT outcome FROM job_keys WHERE kind = 'ingest.import'") == "dead"
        assert await w.scalar("SELECT error_code FROM job_keys WHERE kind = 'ingest.import'") == "upload_missing"
        assert int(await w.scalar("SELECT count(*) FROM materials")) == 0


async def test_converter_route(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        async with api(w) as c:
            assert (await c.get("/api/materials/converter")).json()["state"] == "offline"
        await cmd(w, "next", "--peek", "--wait", "1", *host_args(state="setup_missing", strays=2))
        async with api(w) as c:
            seen = (await c.get("/api/materials/converter")).json()
        assert {k: seen[k] for k in ("state", "strays", "busy", "stale", "converter_version")} == {
            "state": "setup_missing", "strays": 2, "busy": "none", "stale": False, "converter_version": "docling-2.151.1+r2",
        }
        await cmd(w, "next", "--peek", "--wait", "1", *host_args(busy="cli_running"))
        async with api(w, converter_source_sha256="d" * 64) as c:
            seen = (await c.get("/api/materials/converter")).json()
        assert (seen["state"], seen["busy"], seen["stale"]) == ("online", "cli_running", True)
        from sqlalchemy import text

        async with w.engine.begin() as conn:
            await conn.execute(text("UPDATE ingest_hosts SET seen_at = now() - interval '60 seconds'"))
        async with api(w) as c:
            assert (await c.get("/api/materials/converter")).json()["state"] == "offline"
        async with api(w, handle="anna") as c:
            assert (await c.get("/api/materials/converter")).status_code == 403


async def test_upload_routes_are_owner_only(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        view = await uploaded(w, pdf(b"owner"))
        async with api(w, handle="anna") as c:
            for response in (
                await c.get(f"/api/materials/uploads/{view['id']}"),
                await c.post(f"/api/materials/uploads/{view['id']}/cancel"),
                await c.post(f"/api/materials/uploads/{view['id']}/retry"),
                await c.delete(f"/api/materials/uploads/{view['id']}"),
            ):
                assert (response.status_code, response.json()) == (403, {"error": "owner_only"})
        async with api(w) as c:
            assert (await c.get("/api/materials/uploads/not-a-uuid")).status_code == 404
            assert (await c.get(f"/api/materials/uploads/{uuid.uuid4()}")).status_code == 404
