"""SRV-CONFIG settings view and overlay against Postgres, through the api (FR-CONFIG-25, FR-CONFIG-26).

@trace FR-CONFIG-25 FR-CONFIG-26
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from settings_helpers import Pg, write_config

pytestmark = pytest.mark.anyio
ORIGIN = "https://localhost"
SESSION = "__Host-reader_session"
CONFIG = """\
version: 1
instance: { url: "https://localhost", data_dir: ./data }
users:
  - { handle: ivan, role: owner }
  - { handle: anna, role: learner }
tasks:
  draft: { model: opus, budget_usd_per_lesson: 2.0 }
  tutor: { model: sonnet }
budgets: { monthly_usd: 200, warn_at: 0.8 }
sharing: SHARING
"""


class World:
    def __init__(self, path: Path, settings: Any, engine: Any, users: dict[str, Any]) -> None:
        self.path, self.settings, self.engine, self.users = path, settings, engine, users


async def world(pg: Pg, tmp: Path, sharing: str = "{}") -> World:
    from sqlalchemy import text

    from reader.auth.policy import settings_from_config
    from reader.auth.store import prepare
    from reader.config import Secret, load_config
    from reader.db import DbTarget, make_engine, migrate

    path = write_config(tmp, CONFIG.replace("SHARING", sharing))
    settings = settings_from_config(load_config(path))
    engine = make_engine(DbTarget(pg.host, pg.port, pg.user, pg.database, Secret(pg.password)))
    await migrate(engine)
    await prepare(engine, settings)
    async with engine.connect() as conn:
        users = {r[0]: r[1] for r in await conn.execute(text("SELECT handle, id FROM users"))}
    return World(path, settings, engine, users)


async def session_for(w: World, handle: str, age_s: int = 0) -> str:
    from reader.auth.store import _start_session

    async with w.engine.begin() as conn:
        session = await _start_session(conn, w.users[handle], None)
    if age_s:
        await execute(
            w.engine,
            "UPDATE sessions SET created_at = now() - make_interval(secs => :a) WHERE id_sha256 = :h",
            a=age_s,
            h=hashlib.sha256(session.encode()).digest(),
        )
    return session


@asynccontextmanager
async def client(w: World, session: str | None) -> AsyncIterator[httpx.AsyncClient]:
    from reader.api.app import create_app

    app = create_app(w.settings, w.engine, run_cleanup_task=False)
    app.state.reader_config_path = w.path
    cookies = {SESSION: session} if session else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers={"Origin": ORIGIN}, cookies=cookies
    ) as c:
        yield c


async def scalar(engine: Any, sql: str, **params: Any) -> Any:
    from sqlalchemy import text

    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar()


async def execute(engine: Any, sql: str, **params: Any) -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text(sql), params)


async def audit(engine: Any) -> list[tuple[str, str, Any]]:
    from sqlalchemy import text

    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT action, object_id, detail FROM audit_log ORDER BY id"))
        return [(r[0], r[1], r[2]) for r in rows]


async def overrides(engine: Any) -> list[tuple[str, Any, int]]:
    from sqlalchemy import text

    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT key, value, version FROM settings_overrides ORDER BY key"))
        return [(r[0], r[1], r[2]) for r in rows]


def row(body: dict[str, Any], name: str) -> dict[str, Any]:
    return next(r for r in body["rows"] if r["name"] == name)


# FR-CONFIG-25 ----------------------------------------------------------------------------------------------


async def test_view_is_owner_only_and_needs_the_file(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    async with client(w, await session_for(w, "anna")) as c:
        r = await c.get("/api/settings")
        assert (r.status_code, r.json()) == (403, {"error": "owner_only"})
    async with client(w, None) as c:
        assert (await c.get("/api/settings")).status_code == 401
    async with client(w, await session_for(w, "ivan")) as c:
        r = await c.get("/api/settings")
        assert r.status_code == 200
        assert row(r.json(), "signup")["value"] == "open"  # absent on localhost: the api's startup value
        w.path.unlink()
        r = await c.get("/api/settings")
        assert (r.status_code, r.json()) == (503, {"error": "config_unavailable"})
    await w.engine.dispose()


# FR-CONFIG-26 ----------------------------------------------------------------------------------------------


async def test_set_stale_and_reset(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    async with client(w, await session_for(w, "ivan")) as c:
        r = await c.post("/api/settings/budgets.monthly_usd", json={"value": 300, "version": 0})
        assert (r.status_code, r.json()) == (200, {"key": "budgets.monthly_usd", "value": 300, "version": 1})
        stale = await c.post("/api/settings/budgets.monthly_usd", json={"value": 400, "version": 0})
        assert (stale.status_code, stale.json()) == (409, {"error": "stale"})
        r = await c.post("/api/settings/budgets.monthly_usd", json={"value": 150.5, "version": 1})
        assert (r.status_code, r.json()["version"]) == (200, 2)
        view = (await c.get("/api/settings")).json()
        monthly = row(view, "budgets.monthly_usd")
        assert (monthly["value"], monthly["file_value"], monthly["source"], monthly["version"]) == (
            150.5, 200, "override", 2)
        assert monthly["ignored"] is None
        same = await c.post("/api/settings/budgets.monthly_usd", json={"value": 150.5, "version": 2})
        assert (same.status_code, same.json()["version"]) == (200, 2)  # a no-op: no write, no audit row
        reset = await c.request("DELETE", "/api/settings/budgets.monthly_usd", json={})
        assert reset.status_code == 204
        again = await c.request("DELETE", "/api/settings/budgets.monthly_usd", json={})
        assert again.status_code == 204  # idempotent
        monthly = row((await c.get("/api/settings")).json(), "budgets.monthly_usd")
        assert (monthly["value"], monthly["source"], monthly["version"]) == (200, "reader.yaml", 0)
    assert await overrides(w.engine) == []
    owner = str(w.users["ivan"])
    assert await audit(w.engine) == [
        ("settings.set", "budgets.monthly_usd", {"old": None, "new": 300}),
        ("settings.set", "budgets.monthly_usd", {"old": 300, "new": 150.5}),
        ("settings.reset", "budgets.monthly_usd", {"old": 150.5, "file": 200}),
    ]
    assert str(await scalar(w.engine, "SELECT DISTINCT actor_id FROM audit_log")) == owner
    await w.engine.dispose()


async def test_refusals_write_nothing(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path, sharing='{ signup: "off" }')
    async with client(w, await session_for(w, "ivan")) as c:
        refused = ("kernels", "users", "tasks.draft.model", "tasks.gone.budget_usd_per_lesson", "instance")
        for key in refused:
            r = await c.post(f"/api/settings/{key}", json={"value": 1, "version": 0})
            assert (r.status_code, r.json()) == (400, {"error": "not_overridable"}), key
            r = await c.request("DELETE", f"/api/settings/{key}", json={})
            assert (r.status_code, r.json()) == (400, {"error": "not_overridable"}), key
        for key, value in [
            ("observability.retention_days", 0),
            ("observability.retention_days", 366),
            ("observability.retention_days", "30"),
            ("observability.retention_days", True),
            ("budgets.monthly_usd", -1),
            ("budgets.monthly_usd", "secret://env/X"),
            ("budgets.warn_at", 1.5),
            ("tasks.draft.budget_usd_per_lesson", 0),
            ("sharing.signup", "maybe"),
        ]:
            r = await c.post(f"/api/settings/{key}", json={"value": value, "version": 0})
            assert (r.status_code, r.json()) == (400, {"error": "value_invalid", "field": key}), (key, value)
        bodies = ({"value": 1}, {"version": 0}, {"value": 1, "version": -1}, {"value": 1, "version": "0"}, [])
        for body in bodies:
            r = await c.post("/api/settings/budgets.monthly_usd", json=body)
            assert r.status_code == 400, body
        narrower = await c.post("/api/settings/sharing.signup", json={"value": "open", "version": 0})
        assert (narrower.status_code, narrower.json()) == (409, {"error": "file_narrower"})
    async with client(w, await session_for(w, "ivan", age_s=900)) as c:
        r = await c.post("/api/settings/budgets.monthly_usd", json={"value": 1, "version": 0})
        assert (r.status_code, r.json()) == (401, {"error": "reauth_required"})
    async with client(w, await session_for(w, "anna")) as c:
        r = await c.post("/api/settings/budgets.monthly_usd", json={"value": 1, "version": 0})
        assert (r.status_code, r.json()) == (403, {"error": "owner_only"})
        r = await c.request("DELETE", "/api/settings/budgets.monthly_usd", json={})
        assert (r.status_code, r.json()) == (403, {"error": "owner_only"})
    assert await overrides(w.engine) == []
    assert await audit(w.engine) == []
    await w.engine.dispose()


async def test_override_of_a_removed_task_is_ignored_and_logged_once(
    pg: Pg, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from reader.config import effective, load_config

    w = await world(pg, tmp_path)
    async with client(w, await session_for(w, "ivan")) as c:
        r = await c.post("/api/settings/tasks.draft.budget_usd_per_lesson", json={"value": 3, "version": 0})
        assert r.status_code == 200
    draft = "  draft: { model: opus, budget_usd_per_lesson: 2.0 }\n"
    w.path.write_text(CONFIG.replace("SHARING", "{}").replace(draft, ""), encoding="utf-8")
    config = load_config(w.path)
    caplog.set_level(logging.INFO, logger="reader.config")
    effective.reset_logged()
    async with w.engine.connect() as conn:
        first = await effective.effective_settings(conn, config)
        second = await effective.effective_settings(conn, config)
    assert first.ignored == second.ignored == {"tasks.draft.budget_usd_per_lesson": "not_in_file"}
    assert "draft" not in first.task_budgets
    messages = [r.getMessage() for r in caplog.records if "override_ignored" in r.getMessage()]
    assert messages == ["config override_ignored tasks.draft.budget_usd_per_lesson not_in_file"]
    async with client(w, await session_for(w, "ivan")) as c:
        view = (await c.get("/api/settings")).json()
    assert view["ignored"] == [
        {"key": "tasks.draft.budget_usd_per_lesson", "value": 3, "reason": "not_in_file", "version": 1}
    ]
    assert await overrides(w.engine) == [("tasks.draft.budget_usd_per_lesson", 3, 1)]  # kept until reset
    await w.engine.dispose()


async def test_signup_turned_off_by_an_override_answers_404_without_a_restart(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    async with client(w, await session_for(w, "ivan")) as owner, client(w, None) as visitor:
        assert (await visitor.get("/api/auth/signup")).status_code == 200
        assert 'href="/api/auth/signup"' in (await visitor.get("/api/auth/signin")).text
        r = await owner.post("/api/settings/sharing.signup", json={"value": "off", "version": 0})
        assert r.status_code == 200
        assert (await visitor.get("/api/auth/signup")).status_code == 404
        opts = await visitor.post("/api/auth/signup/options", json={"handle": "lc-x", "display_name": "X"})
        assert (opts.status_code, opts.json()) == (404, {"error": "not_found"})
        assert 'href="/api/auth/signup"' not in (await visitor.get("/api/auth/signin")).text
        view = (await owner.get("/api/settings")).json()
        assert (row(view, "signup")["value"], row(view, "signup")["file_value"]) == ("off", "open")
        assert (await owner.request("DELETE", "/api/settings/sharing.signup", json={})).status_code == 204
        assert (await visitor.get("/api/auth/signup")).status_code == 200
    assert await scalar(w.engine, "SELECT count(*) FROM auth_challenges WHERE kind = 'signup'") == 0
    await w.engine.dispose()


async def test_settings_overrides_is_a_system_table(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    columns = await scalar(
        w.engine,
        "SELECT string_agg(column_name || ':' || data_type, ',' ORDER BY ordinal_position)"
        " FROM information_schema.columns WHERE table_name = 'settings_overrides'",
    )
    assert columns == ("key:text,value:jsonb,updated_by:uuid,updated_at:timestamp with time zone,"
                       "version:integer")
    foreign = await scalar(
        w.engine,
        "SELECT count(*) FROM pg_constraint"
        " WHERE conrelid = 'settings_overrides'::regclass AND contype = 'f'",
    )
    assert foreign == 0  # updated_by has no foreign key: it never blocks SRV-AUTH FR-AUTH-43
    await w.engine.dispose()
