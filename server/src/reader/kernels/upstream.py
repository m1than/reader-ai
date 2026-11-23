"""Calls to the user's kernel service, treated as untrusted (SRV-KERNEL FR-KERNEL-7, -13).

`http` is a minimal HTTP/1.1 client: one request per connection, a 10 s timeout, no redirects (a 3xx
is a failure), at most 64 KiB of response body. `ws_connect` opens the gateway's kernel channels
WebSocket without a subprotocol, without proxies, with only the handshake headers, a 10 s connect
timeout, no redirects and a 4 MiB receive limit enforced while receiving.
"""

from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import dataclass
from typing import Any, Final

from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as _connect

from reader.kernels.protocol import UPSTREAM_MESSAGE_MAX, service_name

HTTP_TIMEOUT_S: Final = 10.0
BODY_MAX: Final = 64 * 1024
HEAD_MAX: Final = 16 * 1024
GATEWAY_PORT: Final = 8888
CONTROL_PORT: Final = 8889


class UpstreamError(Exception):
    """Any failed upstream call: connection, timeout, redirect, oversized or malformed response."""


class UpstreamUnresolvable(UpstreamError):
    """The service name does not resolve: the kernels profile is not running (FR-KERNEL-13)."""


@dataclass(frozen=True)
class Endpoint:
    host: str
    gateway_port: int = GATEWAY_PORT
    control_port: int = CONTROL_PORT


def default_resolve(handle: str) -> Endpoint:
    """FR-KERNEL-1: the host comes only from the authenticated handle."""
    return Endpoint(service_name(handle))


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except ValueError:
            raise UpstreamError("malformed body") from None


async def _http(method: str, host: str, port: int, path: str, body: bytes | None) -> Reply:
    reader, writer = await asyncio.open_connection(host, port)
    try:
        data = body or b""
        head = [
            f"{method} {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Connection: close",
            "Accept: application/json",
        ]
        if body is not None:
            head += ["Content-Type: application/json", f"Content-Length: {len(data)}"]
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode("ascii") + data)
        await writer.drain()
        raw_head = await reader.readuntil(b"\r\n\r\n")
        if len(raw_head) > HEAD_MAX:
            raise UpstreamError("head too large")
        lines = raw_head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ", 2)
        if len(parts) < 2 or not parts[1].isdigit():
            raise UpstreamError("bad status line")
        status = int(parts[1])
        if 300 <= status < 400:
            raise UpstreamError("redirect")
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        if "transfer-encoding" in headers:
            payload = await _read_chunked(reader)
        elif "content-length" in headers:
            if not headers["content-length"].isdigit():
                raise UpstreamError("bad length")
            length = int(headers["content-length"])
            if length > BODY_MAX:
                raise UpstreamError("body too large")
            payload = await reader.readexactly(length)
        else:
            payload = await reader.read(BODY_MAX + 1)
            if len(payload) > BODY_MAX:
                raise UpstreamError("body too large")
        return Reply(status, payload)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, UnicodeError, ValueError) as e:
        raise UpstreamError(type(e).__name__) from None
    finally:
        writer.close()


async def _read_chunked(reader: asyncio.StreamReader) -> bytes:
    out = bytearray()
    while True:
        size_line = await reader.readuntil(b"\r\n")
        size = int(size_line.split(b";", 1)[0].strip() or b"0", 16)
        if size == 0:
            await reader.readuntil(b"\r\n")
            return bytes(out)
        if len(out) + size > BODY_MAX:
            raise UpstreamError("body too large")
        out += await reader.readexactly(size)
        await reader.readexactly(2)


async def http(method: str, host: str, port: int, path: str, body: Any = None) -> Reply:
    """One upstream HTTP call; raises UpstreamError on any failure (FR-KERNEL-13)."""
    data = None if body is None else json.dumps(body).encode()
    try:
        async with asyncio.timeout(HTTP_TIMEOUT_S):
            return await _http(method, host, port, path, data)
    except UpstreamError:
        raise
    except socket.gaierror:
        raise UpstreamUnresolvable("gaierror") from None
    except (OSError, TimeoutError) as e:
        raise UpstreamError(type(e).__name__) from None


class _NoRedirect(_connect):
    """websockets' connect without redirect following (FR-KERNEL-13)."""

    def process_redirect(self, exc: Exception) -> Exception | str:
        return exc


async def ws_connect(endpoint: Endpoint, kernel_id: str) -> ClientConnection:
    """The kernel channels WebSocket (FR-KERNEL-7); raises UpstreamError on any failure."""
    uri = f"ws://{endpoint.host}:{endpoint.gateway_port}/api/kernels/{kernel_id}/channels"
    try:
        return await _NoRedirect(
            uri,
            open_timeout=HTTP_TIMEOUT_S,
            max_size=UPSTREAM_MESSAGE_MAX,
            max_queue=4,
            proxy=None,
            user_agent_header=None,
            compression=None,
            ping_interval=None,
        )
    except Exception as e:  # every handshake failure is an upstream failure
        raise UpstreamError(type(e).__name__) from None
