"""SRV-SEARCH [db]: `POST /api/find` and `POST /api/open` (FR-SEARCH-8, revised) on the owner channel, fixed
by the server; owner isolation through the routes (INV-SEARCH-1).

@trace FR-SEARCH-8 INV-SEARCH-1
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from .conftest import (
    MAT_EN,
    MAT_RU,
    ORIGIN,
    SESSION_COOKIE,
    EmbedServer,
    Pg,
    World,
    add_material,
    embed_material,
    world,
)

pytestmark = pytest.mark.anyio
FILL = " Nothing else is said here about any other matter, this is only filler for the paragraph."
SPANS = [
    (1, "Wallet keys sign every transfer of the owner." + FILL),
    (2, "A miner checks transactions and earns a reward for the block." + FILL),
    (3, "word " * 400),  # 2,000 characters: cut on the owner channel, never on the pipeline's
]
OUTLINE = [(1, "Keys", 1), (1, "Mining", 2)]


@pytest.fixture
async def w(
    pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, embed_server: EmbedServer
) -> AsyncIterator[World]:
    monkeypatch.setenv("READER_EMBED_URL", embed_server.url)
    built = await world(pg, tmp_path, monkeypatch)
    try:
        yield built
    finally:
        await built.engine.dispose()


@asynccontextmanager
async def client(w: World, handle: str = "ivan") -> AsyncIterator[httpx.AsyncClient]:
    from reader.api.app import create_app
    from reader.auth.store import _start_session

    async with w.engine.begin() as conn:
        session = await _start_session(conn, w.users[handle], None)
    app = create_app(w.settings, w.engine, run_cleanup_task=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url=ORIGIN,
        headers={"Origin": ORIGIN}, cookies={SESSION_COOKIE: session},
    ) as c:
        yield c


async def material(w: World, handle: str = "ivan", sha: str = MAT_EN, spans: Any = SPANS) -> None:
    ingest = await add_material(w, handle, sha, spans, config_id="book-en", outline=OUTLINE)
    await embed_material(w, ingest, sha)


async def test_find_and_open_happy_paths(w: World) -> None:
    await material(w)
    async with client(w) as c:
        found = await c.post("/api/find", json={"q": "wallet keys transfer"})
        assert found.status_code == 200, found.text
        body = found.json()
        assert set(body) == {"brain_version", "policy_version", "budget", "cards", "judge_ran", "judge_skip",
                             "degraded"}
        assert body["budget"] == 2000 and body["judge_skip"] == "gate" and body["cards"]  # the shipped gate
        assert {c["source"]["id"] for c in body["cards"]} == {f"src:{MAT_EN}"}
        sized = await c.post("/api/find", json={"q": "wallet", "k": 1, "budget": 99999, "filters": {}})
        assert sized.status_code == 200 and len(sized.json()["cards"]) == 1
        assert sized.json()["budget"] == 10500  # the owner channel's maximum, clamped
        first = body["cards"][0]["id"]
        opened = await c.post("/api/open", json={"id": first})
        assert opened.status_code == 200, opened.text
        assert [x["id"] for x in opened.json()["cards"]] == [first]
        source = await c.post("/api/open", json={"id": f"src:{MAT_EN}", "depth": 2, "budget": 400})
        assert source.status_code == 200 and source.json()["budget"] == 400


async def test_the_channel_is_fixed_by_the_server(w: World) -> None:
    await material(w)
    async with client(w) as c:
        for extra in ({"channel": "pipeline"}, {"channel": "mcp"}, {"question_type": "fact_check"},
                      {"judge_ctx": {"line": "tutor"}}):
            r = await c.post("/api/find", json={"q": "word", **extra})
            assert (r.status_code, r.json()) == (400, {"error": "query_invalid"}), extra
            o = await c.post("/api/open", json={"id": f"src:{MAT_EN}", **extra})
            assert (o.status_code, o.json()) == (400, {"error": "id_invalid"}), extra
        long = (await c.post("/api/find", json={"q": "word word", "budget": 10500})).json()
        texts = [x["text"] for x in long["cards"] if x["text"].startswith("word")]
        assert texts and all(len(t) <= 1200 for t in texts)  # the owner row: cut at the L1 card budget


@pytest.mark.parametrize(
    ("path", "body", "status", "code"),
    [
        ("/api/find", {"q": "   "}, 400, "query_invalid"),
        ("/api/find", {"q": "x" * 1001}, 400, "query_invalid"),
        ("/api/find", {"q": 5}, 400, "query_invalid"),
        ("/api/find", {}, 400, "query_invalid"),
        ("/api/find", [1], 400, "query_invalid"),
        ("/api/find", {"q": "x", "k": 0}, 400, "k_invalid"),
        ("/api/find", {"q": "x", "k": "3"}, 400, "k_invalid"),
        ("/api/find", {"q": "x", "budget": 399}, 400, "budget_invalid"),
        ("/api/find", {"q": "x", "filters": {"colour": "red"}}, 400, "filter_invalid"),
        ("/api/find", {"q": "x", "filters": ["role"]}, 400, "filter_invalid"),
        ("/api/open", {"id": "nonsense"}, 400, "id_invalid"),
        ("/api/open", {"id": 7}, 400, "id_invalid"),
        ("/api/open", {}, 400, "id_invalid"),
        ("/api/open", {"id": f"src:{MAT_EN}", "depth": 9}, 400, "depth_invalid"),
        ("/api/open", {"id": f"src:{MAT_EN}", "budget": 10}, 400, "budget_invalid"),
        ("/api/open", {"id": f"src:{MAT_RU}"}, 404, "not_found"),
        ("/api/open", {"id": "con:anything"}, 404, "not_found"),
    ],
)
async def test_each_error_code(w: World, path: str, body: Any, status: int, code: str) -> None:
    await material(w)
    async with client(w) as c:
        r = await c.post(path, json=body)
    assert (r.status_code, r.json()) == (status, {"error": code})


async def test_learners_get_404(w: World) -> None:
    await material(w)
    async with client(w, "anna") as c:
        for path, body in (("/api/find", {"q": "wallet"}), ("/api/open", {"id": f"src:{MAT_EN}"})):
            r = await c.post(path, json=body)
            assert (r.status_code, r.json()) == (404, {"error": "not_found"})


async def test_cards_come_from_the_callers_own_manifest(w: World) -> None:
    await material(w, "anna", MAT_RU, [(1, "Wallet keys of the learner, her own copy here." + FILL)])
    await material(w)
    async with client(w) as c:
        body = (await c.post("/api/find", json={"q": "wallet keys", "budget": 10500})).json()
        assert body["cards"] and {x["source"]["id"] for x in body["cards"]} == {f"src:{MAT_EN}"}
        assert (await c.post("/api/open", json={"id": f"src:{MAT_RU}"})).status_code == 404


async def test_no_query_or_card_text_in_logs(w: World, caplog: pytest.LogCaptureFixture) -> None:
    canary = "kanareyka" + uuid.uuid4().hex[:8]
    caplog.set_level(logging.DEBUG)
    await material(w, spans=[(1, f"Text about {canary} and wallet keys." + FILL)])
    async with client(w) as c:
        ok = await c.post("/api/find", json={"q": f"{canary} wallet"})
        assert ok.status_code == 200 and canary in ok.text
        await c.post("/api/find", json={"q": canary * 200})
        await c.post("/api/find", json={"q": canary, "filters": {canary: canary}})
        await c.post("/api/open", json={"id": f"spn:{canary}"})
        await c.post("/api/open", json={"id": ok.json()["cards"][0]["id"]})
    assert canary not in caplog.text
    for record in caplog.records:
        assert canary not in record.getMessage() and canary not in repr(record.args)
