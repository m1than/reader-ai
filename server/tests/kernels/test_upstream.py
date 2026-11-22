# @trace FR-KERNEL-7, FR-KERNEL-12, FR-KERNEL-13, VEC-KERNEL-1
"""SRV-KERNEL's upstream client against local servers that behave like an untrusted kernel service: no
redirects (REST and WebSocket), the 64 KiB body cap, the timeout, the fixed handshake headers, and the 4 MiB
receive limit enforced while receiving (close 1009)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest

from .conftest import load

pytestmark = pytest.mark.anyio
Handler = Callable[[str, str], tuple[int, dict[str, str], bytes] | None]


@asynccontextmanager
async def http_server(handler: Handler) -> AsyncIterator[tuple[int, list[dict[str, Any]]]]:
    seen: list[dict[str, Any]] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1").split("\r\n")
        method, path, _ = head[0].split(" ", 2)
        headers = {k.strip().lower(): v.strip() for k, _, v in (h.partition(":") for h in head[1:] if h)}
        body = await reader.readexactly(int(headers.get("content-length", "0")))
        seen.append({"method": method, "path": path, "headers": headers, "body": body})
        answer = handler(method, path)
        if answer is None:
            await asyncio.sleep(60)
            return
        status, extra, payload = answer
        lines = [f"HTTP/1.1 {status} X", *(f"{k}: {v}" for k, v in extra.items())]
        if "Transfer-Encoding" not in extra:
            lines.append(f"Content-Length: {len(payload)}")
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + payload)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port, seen
    finally:
        server.close()


def _routes(method: str, path: str) -> tuple[int, dict[str, str], bytes] | None:
    if path == "/ok":
        return 200, {}, b'{"nonce": "a", "gateway": "up"}'
    if path == "/redirect":
        return 302, {"Location": "http://127.0.0.1:1/elsewhere"}, b""
    if path == "/big":
        return 200, {}, b"x" * (65 * 1024)
    if path == "/chunked":
        return 200, {"Transfer-Encoding": "chunked"}, b"5\r\nhello\r\n0\r\n\r\n"
    if path == "/slow":
        return None
    return 404, {}, b""


async def test_http_client_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = load("reader.kernels.upstream")
    async with http_server(_routes) as (port, seen):
        ok = await upstream.http("POST", "127.0.0.1", port, "/ok", {"name": "python3"})
        assert ok.status == 200
        assert ok.json()["gateway"] == "up"
        assert json.loads(seen[0]["body"]) == {"name": "python3"}
        assert set(seen[0]["headers"]) <= {"host", "connection", "accept", "content-type", "content-length"}
        assert (await upstream.http("GET", "127.0.0.1", port, "/chunked")).body == b"hello"
        assert (await upstream.http("GET", "127.0.0.1", port, "/missing")).status == 404
        for path in ("/redirect", "/big"):
            with pytest.raises(upstream.UpstreamError):
                await upstream.http("GET", "127.0.0.1", port, path)
        assert [s["path"] for s in seen].count("/redirect") == 1
        monkeypatch.setattr(upstream, "HTTP_TIMEOUT_S", 0.3)
        with pytest.raises(upstream.UpstreamError):
            await upstream.http("GET", "127.0.0.1", port, "/slow")
    with pytest.raises(upstream.UpstreamError):
        await upstream.http("GET", "127.0.0.1", port, "/ok")       # server gone: connection refused
    with pytest.raises(upstream.UpstreamError, match="gaierror"):
        await upstream.http("GET", "kernel-user-nobody.invalid", 8889, "/control/state")


@asynccontextmanager
async def ws_server(
    handler: Callable[[Any], Any], process_request: Any = None
) -> AsyncIterator[tuple[int, list[dict[str, str]]]]:
    from websockets.asyncio.server import serve

    seen: list[dict[str, str]] = []

    def record(connection: Any, request: Any) -> Any:
        seen.append({k.lower(): v for k, v in request.headers.raw_items()})
        return process_request(connection, request) if process_request else None

    async with serve(handler, "127.0.0.1", 0, process_request=record, max_size=None) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        yield port, seen


async def test_ws_connect_headers_and_receive_limit() -> None:
    upstream = load("reader.kernels.upstream")
    from websockets.exceptions import ConnectionClosed

    async def handler(ws: Any) -> None:
        await ws.send("small")
        await ws.send("y" * (4 * 1024 * 1024 + 1))
        await asyncio.sleep(1)

    async with ws_server(handler) as (port, seen):
        conn = await upstream.ws_connect(upstream.Endpoint("127.0.0.1", port, 1), "k")
        assert await conn.recv() == "small"
        with pytest.raises(ConnectionClosed) as info:
            await conn.recv()
        assert info.value.sent is not None
        assert info.value.sent.code == 1009
    headers = seen[0]
    assert set(headers) == {"host", "upgrade", "connection", "sec-websocket-key", "sec-websocket-version"}


async def test_ws_connect_follows_no_redirect() -> None:
    upstream = load("reader.kernels.upstream")

    def redirect(connection: Any, request: Any) -> Any:
        response = connection.respond(301, "moved")
        response.headers["Location"] = "/api/kernels/other/channels"
        return response

    async def handler(ws: Any) -> None:
        await ws.send("should not be reached")

    async with ws_server(handler, redirect) as (port, seen):
        with pytest.raises(upstream.UpstreamError):
            await upstream.ws_connect(upstream.Endpoint("127.0.0.1", port, 1), "k")
    assert len(seen) == 1
