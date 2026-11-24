"""SRV-AUTH FR-AUTH-33 (paired SRV-OBS change): the obs routes, the trace middleware, setup in reader-server,
and the owner gate on real sessions (SRV-OBS FR-OBS-9, INV-OBS-5). [unit, db]

@trace FR-AUTH-33 FR-OBS-9 INV-OBS-5 FR-OBS-1 FR-OBS-4
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from auth_helpers import ORIGIN, bootstrap, bootstrap_token, client, ready_engine, settings_for, write_config
from conftest import Pg
from softauthn import SoftAuthenticator

pytestmark = pytest.mark.anyio
DASH = "https://localhost:8443"


def test_routers_and_middleware_order(tmp_path: Path) -> None:
    from reader.api import app as appmod

    assert "reader.obs.routes" in appmod.ROUTERS
    paths = {(r.method, r.path) for r in appmod.ROUTES}
    assert {("GET", "/api/obs/gate"), ("GET", "/api/obs/summary")} <= paths
    app = appmod.create_app(settings_for(tmp_path), engine=None, run_cleanup_task=False)  # type: ignore[arg-type]
    names = [m.cls.__name__ for m in app.user_middleware]
    assert names[0] == "TraceMiddleware"  # outermost: outside the CSRF gate
    assert "CsrfGate" in names


def test_serving_logs_installs_the_trace_filter() -> None:
    from reader.serve.main import serving_logs

    reader, root = logging.getLogger("reader"), logging.getLogger()
    saved = (list(reader.handlers), reader.propagate, reader.level, list(root.handlers), root.level)
    try:
        serving_logs()
        filters = [f for h in reader.handlers for f in h.filters]
        assert any(type(f).__name__ == "TraceFilter" for f in filters)
    finally:
        reader.handlers[:], reader.propagate, reader.level = saved[0], saved[1], saved[2]
        root.handlers[:], root.level = saved[3], saved[4]


def test_reader_server_starts_and_stops_tracing(tmp_path: Path) -> None:
    from reader.serve import main as serve

    for enabled in (True, False):
        path = write_config(
            tmp_path / str(enabled),
            'version: 1\ninstance: { url: "https://localhost", data_dir: ./data }\n'
            f"observability: {{ enabled: {str(enabled).lower()} }}\n",
        )
        env = serve.Env.read({"READER_CONFIG": str(path)})
        handle = serve.start_obs("api", env)
        try:
            assert handle.enabled is enabled
        finally:
            handle.shutdown()


async def _sessions(pg: Pg, tmp: Path) -> tuple[Any, Any, dict[str, str]]:
    settings = settings_for(tmp)
    engine = await ready_engine(pg, settings)
    cookies: dict[str, str] = {}
    for handle in ("ivan", "live-check"):
        async with client(settings, engine) as c:
            response, _ = await bootstrap(
                c, await bootstrap_token(engine, settings, handle), SoftAuthenticator.random()
            )
            assert response.status_code == 200, response.text
            cookies[handle] = c.cookies.get("__Host-reader_session") or ""
    return settings, engine, cookies


async def _gate(
    settings: Any, engine: Any, cookie: str | None, query: str = "max_results=1", **headers: str
) -> httpx.Response:
    from reader.api.app import create_app

    app = create_app(settings, engine, run_cleanup_task=False)
    jar = {"__Host-reader_session": cookie} if cookie else {}
    # SRV-OBS FR-OBS-9: Caddy names the site on every gate request; the 8443 site unless a test says otherwise
    # (an empty value sends no header).
    sent = {k: v for k, v in {"X-Reader-Site": "mlflow", **headers}.items() if v}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN, cookies=jar) as c:
        return await c.get(f"/api/obs/gate?{query}", headers=sent)


async def test_gate_on_real_sessions(pg: Pg, tmp_path: Path) -> None:
    settings, engine, cookies = await _sessions(pg, tmp_path)
    owner, learner = cookies["ivan"], cookies["live-check"]
    try:
        ok = await _gate(settings, engine, owner)
        assert (ok.status_code, ok.content) == (204, b"")
        assert "set-cookie" not in ok.headers
        assert (await _gate(settings, engine, None)).status_code == 401
        assert (await _gate(settings, engine, "not-a-session")).status_code == 401
        assert (await _gate(settings, engine, learner)).status_code == 403
        assert (await _gate(settings, engine, owner, Origin="https://localhost:3000")).status_code == 403
        assert (await _gate(settings, engine, owner, **{"X-Forwarded-Method": "POST"})).status_code == 403
        assert (
            await _gate(settings, engine, owner, Origin=DASH, **{"X-Forwarded-Method": "POST"})
        ).status_code == 204
        nav = {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
        assert (await _gate(settings, engine, owner, **nav)).status_code == 204
        img = {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "image"}
        assert (await _gate(settings, engine, owner, **img)).status_code == 403
        # The gate reads headers only: Caddy appends the original query, which never stands in for them.
        spoof = "origin=https://localhost:8443&method=GET&x-forwarded-method=GET"
        post = {"X-Forwarded-Method": "POST"}
        assert (await _gate(settings, engine, owner, spoof, **post)).status_code == 403
        assert (await _gate(settings, engine, owner, spoof)).status_code == 204
        for origin in ("HTTPS://LOCALHOST:8443", "https://localhost:8443/"):
            refused = await _gate(settings, engine, owner, Origin=origin, **post)
            assert refused.status_code == 403, origin
        # Per site (SRV-OBS FR-OBS-9, -19): no site header is refused; each site accepts only its own origin.
        site = "X-Reader-Site"
        assert (await _gate(settings, engine, owner, **{site: ""})).status_code == 403
        jaeger = {site: "jaeger", "Origin": "https://localhost:8444", **post}
        assert (await _gate(settings, engine, owner, **jaeger)).status_code == 204
        assert (await _gate(settings, engine, owner, **{**jaeger, "Origin": DASH})).status_code == 403
        wrong = await _gate(settings, engine, owner, Origin="https://localhost:8444", **post)  # site mlflow
        assert wrong.status_code == 403
        assert (await _gate(settings, engine, owner, **{site: "app"})).status_code == 204
    finally:
        await engine.dispose()


async def test_app_refuses_the_dashboard_origin(pg: Pg, tmp_path: Path) -> None:
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from reader.api.app import create_app

    settings, engine, cookies = await _sessions(pg, tmp_path)
    try:
        app = create_app(settings, engine, run_cleanup_task=False)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=ORIGIN,
            cookies={"__Host-reader_session": cookies["ivan"]},
        ) as c:
            r = await c.post("/api/kernel/sessions", json={"lesson_id": "w01"}, headers={"Origin": DASH})
            assert (r.status_code, r.json()) == (403, {"error": "origin"})
        with TestClient(app, base_url=ORIGIN) as tc:
            tc.cookies.set("__Host-reader_session", cookies["ivan"])
            with (
                pytest.raises(WebSocketDisconnect) as closed,
                tc.websocket_connect(
                    "/api/kernel/sessions/00000000-0000-4000-8000-000000000001/channels",
                    headers={"Origin": DASH},
                ),
            ):
                pass
            assert closed.value.code == 1008
    finally:
        await engine.dispose()


async def test_summary_route_is_owner_only(pg: Pg, tmp_path: Path) -> None:
    from reader.api.app import create_app

    settings, engine, cookies = await _sessions(pg, tmp_path)
    try:
        app = create_app(settings, engine, run_cleanup_task=False)
        for cookie, status in ((None, 401), (cookies["live-check"], 403), (cookies["ivan"], 200)):
            jar = {"__Host-reader_session": cookie} if cookie else {}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=ORIGIN, cookies=jar
            ) as c:
                r = await c.get("/api/obs/summary")
                assert r.status_code == status, cookie
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=ORIGIN,
            cookies={"__Host-reader_session": cookies["ivan"]},
        ) as c:
            body = (await c.get("/api/obs/summary?days=7")).json()
            # FR-OBS-17 adds `lessons`; phase 2 adds `alerts` (FR-OBS-25) and `stack` (FR-OBS-32)
            assert set(body) == {"days", "compile_runs", "tutor", "failures", "ingest", "lessons", "alerts",
                                 "stack"}
            for bad in ("0", "91", "x", "²", "٣", "007", "-1", "+5", " 7", "7 ", ""):  # "²": round 1, a 500
                assert (await c.get(f"/api/obs/summary?days={bad}")).status_code == 400
    finally:
        await engine.dispose()
