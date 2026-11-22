# @trace FR-KERNEL-1, FR-KERNEL-2, FR-KERNEL-3, FR-KERNEL-7, FR-KERNEL-11, FR-AUTH-10
"""SRV-KERNEL routes through the real app (SRV-AUTH's dispatcher, CSRF gate and sessions on Postgres), with
the kernel service's upstream faked: authentication, Origin, bodies, the 404 for foreign sessions, the
WebSocket relay end to end, the connection limit, the unusable-service notice and the session re-check."""

from __future__ import annotations

import asyncio
import json
import textwrap
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from .conftest import (
    ORIGIN,
    FakeClock,
    FakeUpstream,
    Pg,
    execute,
    kernel_msg,
    load,
    requires_docker,
    tick_sleep,
)

COOKIE = "__Host-reader_session"
CONFIG = """\
version: 1
instance: { url: "https://localhost", data_dir: ./data }
users:
  - { handle: ivan, role: owner }
  - { handle: anna, role: learner }
"""
USER_ID = "SELECT id FROM users WHERE handle = :h"


@contextmanager
def stack(pg: Pg, tmp: Path) -> Iterator[tuple[Any, Any, FakeUpstream, FakeClock, dict[str, str]]]:
    """The app on a migrated database, a fake kernel upstream, and session cookies for ivan and anna."""
    from starlette.testclient import TestClient

    app_mod = load("reader.api.app")
    service_mod = load("reader.kernels.service")
    from sqlalchemy import text

    from reader.auth import store
    from reader.auth.policy import settings_from_config
    from reader.config import Secret, load_config
    from reader.db import DbTarget, make_engine, migrate

    path = tmp / "reader.yaml"
    path.write_text(textwrap.dedent(CONFIG), encoding="utf-8")
    settings = settings_from_config(load_config(path))
    engine = make_engine(DbTarget(pg.host, pg.port, pg.user, pg.database, Secret(pg.password)))
    app = app_mod.create_app(settings, engine, run_cleanup_task=False)
    app.state.test_engine = engine
    up = FakeUpstream()
    clock = FakeClock()
    app.state.kernel_service = service_mod.KernelService(
        up.endpoint, clock=clock, sleep=tick_sleep, http=up.http, ws=up.ws
    )

    async def setup() -> dict[str, str]:
        await migrate(engine)
        await store.prepare(engine, settings)
        cookies = {}
        async with engine.begin() as conn:
            for handle in ("ivan", "anna"):
                uid = (await conn.execute(text(USER_ID), {"h": handle})).scalar()
                cookies[handle] = await store._start_session(conn, uid, None)
        return cookies

    with TestClient(app, base_url=ORIGIN) as client:
        cookies = client.portal.call(setup)
        try:
            yield client, app, up, clock, cookies
        finally:
            client.portal.call(engine.dispose)


def _h(cookie: str | None, origin: str | None = ORIGIN, **extra: str) -> dict[str, str]:
    headers = dict(extra)
    if cookie is not None:
        headers["cookie"] = f"{COOKIE}={cookie}"
    if origin is not None:
        headers["origin"] = origin
    return headers


def _refused(c: Any, path: str, headers: dict[str, str]) -> tuple[str, int]:
    """How an upgrade was refused: ("denied", HTTP status) or ("closed", close code)."""
    from starlette.testclient import WebSocketDenialResponse
    from starlette.websockets import WebSocketDisconnect

    try:
        with c.websocket_connect(path, headers=headers) as ws:
            ws.receive_text()
    except WebSocketDenialResponse as e:
        return "denied", int(e.status_code)
    except WebSocketDisconnect as e:
        return "closed", int(e.code)
    raise AssertionError("the upgrade was accepted")


def _closed_with(ws: Any, limit: int = 100) -> int:
    from starlette.websockets import WebSocketDisconnect

    for _ in range(limit):
        try:
            ws.receive_text()
        except WebSocketDisconnect as e:
            return int(e.code)
    raise AssertionError("still open")


def test_routers_include_kernel_routes_as_data() -> None:
    app_mod = load("reader.api.app")
    routes = load("reader.kernels.routes")
    assert "reader.kernels.routes" in app_mod.ROUTERS
    declared = {(r.method, r.path, r.auth) for r in routes.ROUTES}
    assert declared == {
        ("POST", "/api/kernel/sessions", "session"),
        ("GET", "/api/kernel/sessions", "session"),
        ("DELETE", "/api/kernel/sessions/{session_id}", "session"),
        ("POST", "/api/kernel/sessions/{session_id}/interrupt", "session"),
        ("POST", "/api/kernel/reset", "session"),
    }
    assert [r.path for r in routes.WS_ROUTES] == ["/api/kernel/sessions/{session_id}/channels"]


@requires_docker
def test_authentication_origin_and_bodies(pg: Pg, tmp_path: Path) -> None:
    with stack(pg, tmp_path) as (c, _, up, _, cookies):
        ivan, anna = cookies["ivan"], cookies["anna"]
        r = c.post("/api/kernel/sessions", json={"lesson_id": "L02"}, headers=_h(None))
        assert (r.status_code, r.json()) == (401, {"error": "unauthenticated"})
        assert c.get("/api/kernel/sessions").status_code == 401
        no_origin = c.post("/api/kernel/sessions", json={"lesson_id": "L02"}, headers=_h(ivan, None))
        assert no_origin.status_code == 403
        assert up.calls == []
        r = c.post("/api/kernel/sessions", json={"lesson_id": "L02"}, headers=_h(ivan))
        assert r.status_code == 201
        sid = r.json()["session_id"]
        listed = c.get("/api/kernel/sessions", headers=_h(ivan)).json()
        assert listed == {"sessions": [{"session_id": sid, "lesson_id": "L02"}]}
        url = f"/api/kernel/sessions/{sid}/interrupt"
        as_json = _h(ivan, **{"content-type": "application/json"})
        as_text = _h(ivan, **{"content-type": "text/plain"})
        assert c.post(url, headers=_h(ivan)).status_code == 204
        assert c.post(url, content=b"{}", headers=as_json).status_code == 204
        assert c.post(url, json={"x": 1}, headers=_h(ivan)).status_code == 415
        assert c.post(url, content=b"{}", headers=as_text).status_code == 415
        big = b"{" + b" " * (65 * 1024) + b"}"
        deep = b"[" * 100_000 + b"]" * 100_000
        assert c.post(url, content=big, headers=as_json).status_code == 415
        assert c.post(url, content=deep, headers=as_json).status_code == 415
        foreign = c.post(url, headers=_h(anna))
        unknown = c.post(f"/api/kernel/sessions/{'0' * 32}/interrupt", headers=_h(anna))
        assert (foreign.status_code, foreign.json()) == (404, {"error": "not_found"})
        assert (unknown.status_code, unknown.json()) == (404, {"error": "not_found"})
        deleted = c.request("DELETE", f"/api/kernel/sessions/{sid}", headers=_h(ivan))
        assert deleted.status_code == 204
        assert {call.host for call in up.calls} == {"kernel-user-ivan"}


async def _sleep() -> None:
    await asyncio.sleep(0.01)


@requires_docker
def test_websocket_relay_end_to_end(pg: Pg, tmp_path: Path) -> None:
    with stack(pg, tmp_path) as (c, _, up, _, cookies):
        ivan = cookies["ivan"]
        made = c.post("/api/kernel/sessions", json={"lesson_id": "L02"}, headers=_h(ivan))
        path = f"/api/kernel/sessions/{made.json()['session_id']}/channels"
        assert _refused(c, path, _h(None)) == ("denied", 401)
        assert _refused(c, path, _h(ivan, "https://evil.example")) == ("closed", 1008)   # FR-AUTH-12 gate
        assert _refused(c, path, _h(cookies["anna"])) == ("denied", 404)
        with c.websocket_connect(path, headers=_h(ivan)) as ws:
            ws.send_text(json.dumps(execute("m1")))
            sock = None
            for _ in range(200):
                sock = next(iter(up.sockets.values()), None)
                if sock is not None and sock.sent:
                    break
                c.portal.call(_sleep)
            assert sock is not None
            assert sock.sent[0]["header"]["username"] == "ivan"
            c.portal.call(sock.push, kernel_msg("shell", "execute_reply", "m1", {"status": "ok"}))
            got = json.loads(ws.receive_text())
            assert got["header"]["msg_type"] == "execute_reply"
            with c.websocket_connect(path, headers=_h(ivan)):
                assert _refused(c, path, _h(ivan)) == ("denied", 409)


@requires_docker
def test_session_recheck_and_unusable_service_notice(pg: Pg, tmp_path: Path) -> None:
    from reader.auth import store

    with stack(pg, tmp_path) as (c, app, up, clock, cookies):
        made = c.post("/api/kernel/sessions", json={"lesson_id": "L02"}, headers=_h(cookies["ivan"]))
        path = f"/api/kernel/sessions/{made.json()['session_id']}/channels"
        with c.websocket_connect(path, headers=_h(cookies["ivan"])) as ws:
            c.portal.call(store.revoke_session, app.state.test_engine, cookies["ivan"])
            clock.advance(61)
            assert _closed_with(ws) == 4401
        # anna's service becomes unusable the real way: her reset fails (the exit is refused).
        anna = cookies["anna"]
        made = c.post("/api/kernel/sessions", json={"lesson_id": "L01"}, headers=_h(anna))
        sid = made.json()["session_id"]
        up.exit_status = 500
        failed = c.post("/api/kernel/reset", headers=_h(anna))
        assert (failed.status_code, failed.json()) == (503, {"error": "kernel_unavailable"})
        with c.websocket_connect(f"/api/kernel/sessions/{sid}/channels", headers=_h(anna)) as ws:
            msg = json.loads(ws.receive_text())
            assert msg["content"]["code"] == "kernel_unavailable"
            assert _closed_with(ws) == 1013


@requires_docker
def test_session_dropped_during_the_handshake_gets_kernel_reset(
    pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from starlette.websockets import WebSocket

    with stack(pg, tmp_path) as (c, app, up, _, cookies):
        made = c.post("/api/kernel/sessions", json={"lesson_id": "L02"}, headers=_h(cookies["ivan"]))
        path = f"/api/kernel/sessions/{made.json()['session_id']}/channels"
        real_accept = WebSocket.accept

        async def accept_after_a_drop(self: Any, *args: Any, **kwargs: Any) -> None:
            app.state.kernel_service.drop_all("ivan", "kernel_reset", 1012)
            await real_accept(self, *args, **kwargs)

        monkeypatch.setattr(WebSocket, "accept", accept_after_a_drop)
        with c.websocket_connect(path, headers=_h(cookies["ivan"])) as ws:
            msg = json.loads(ws.receive_text())
            assert msg["content"]["code"] == "kernel_reset"
            assert _closed_with(ws) == 1012
        assert up.ws_hosts == []       # no upstream connection for a dropped session


class _SlowHandshake:
    """A WebSocket stand-in whose accept takes a while, for concurrent upgrades through `channels`."""

    def __init__(self, app: Any, sid: str, gate: asyncio.Event) -> None:
        self.app, self.gate = app, gate
        self.headers = {"origin": ORIGIN}
        self.cookies = {COOKIE: "c"}
        self.path_params = {"session_id": sid}
        self.scope = {"extensions": {"websocket.http.response": {}}}
        self.client_state = type("S", (), {"name": "CONNECTING"})()
        self.accepted = False
        self.denied: int | None = None
        self.closed: int | None = None

    fail_accept = False

    async def accept(self) -> None:
        await self.gate.wait()
        if self.fail_accept:
            raise ConnectionResetError("the browser went away during the handshake")
        self.accepted = True
        self.client_state = type("S", (), {"name": "CONNECTED"})()

    async def send_denial_response(self, response: Any) -> None:
        self.denied = response.status_code

    async def send_text(self, _: str) -> None:
        return None

    async def close(self, code: int = 1000) -> None:
        self.closed = code

    async def receive(self) -> dict[str, Any]:
        await asyncio.sleep(0.05)
        return {"type": "websocket.disconnect"}


def test_concurrent_upgrades_respect_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    routes = load("reader.kernels.routes")
    service_mod = load("reader.kernels.service")
    from reader.auth import store

    async def run() -> list[_SlowHandshake]:
        up = FakeUpstream()
        clock = FakeClock()
        svc = service_mod.KernelService(up.endpoint, clock=clock, sleep=tick_sleep, http=up.http, ws=up.ws)
        app = type("App", (), {})()
        app.state = type("State", (), {"kernel_service": svc})()
        _, session = await svc.create("ivan", "L02")
        user = type("U", (), {"handle": "ivan"})()

        async def session_user(*_: Any) -> Any:
            return user

        monkeypatch.setattr(store, "session_user", session_user)
        settings = type("Settings", (), {"origin": ORIGIN})()
        gate = asyncio.Event()
        sockets = [_SlowHandshake(app, session.id, gate) for _ in range(3)]
        tasks = [asyncio.ensure_future(routes.channels(ws, settings, None)) for ws in sockets]
        await asyncio.sleep(0.05)       # all three are inside the handshake
        gate.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
        return sockets

    sockets = asyncio.run(run())
    assert sorted(ws.accepted for ws in sockets) == [False, True, True]
    assert [ws.denied for ws in sockets if not ws.accepted] == [409]


def test_hub_starts_when_the_creating_handshake_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-KERNEL-7: the connection that created the hub fails its accept while a second one is attached;
    the second still starts the hub (ensure_started is idempotent)."""
    routes = load("reader.kernels.routes")
    service_mod = load("reader.kernels.service")
    from reader.auth import store

    up = FakeUpstream()

    async def run() -> list[_SlowHandshake]:
        svc = service_mod.KernelService(
            up.endpoint, clock=FakeClock(), sleep=tick_sleep, http=up.http, ws=up.ws
        )
        app = type("App", (), {})()
        app.state = type("State", (), {"kernel_service": svc})()
        _, session = await svc.create("ivan", "L02")
        user = type("U", (), {"handle": "ivan"})()

        async def session_user(*_: Any) -> Any:
            return user

        monkeypatch.setattr(store, "session_user", session_user)
        settings = type("Settings", (), {"origin": ORIGIN})()
        gate = asyncio.Event()
        first, second = _SlowHandshake(app, session.id, gate), _SlowHandshake(app, session.id, gate)
        first.fail_accept = True
        t1 = asyncio.ensure_future(routes.channels(first, settings, None))
        await asyncio.sleep(0.02)
        t2 = asyncio.ensure_future(routes.channels(second, settings, None))
        await asyncio.sleep(0.02)
        gate.set()
        results = await asyncio.wait_for(asyncio.gather(t1, t2, return_exceptions=True), 5)
        assert isinstance(results[0], ConnectionResetError)
        return [first, second]

    first, second = asyncio.run(run())
    assert not first.accepted
    assert second.accepted
    assert up.ws_hosts == ["kernel-user-ivan"]     # the hub was started exactly once
