"""agent-proxy: the credential-injecting reverse proxy of ADR-0025 (SRV-AGENT FR-AGENT-1..5).

It holds provider tokens; the worker gets a short-lived run_token bound to one user and one output cap, and
the CLI sends it as its bearer. agent-proxy swaps it for that user's token and forwards exactly
`POST /v1/messages` (query empty or `beta=true`) to one pinned upstream, with an allow-list of request headers
and two body checks. It records every upstream status per run_token and returns that record on revoke.
Entry point: `python -I -m reader.agent.proxy`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sys
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from reader.config import ConfigError, Secret, SecretRef, SecretResolutionError, load_config
from reader.config.secrets import host_resolved_resolver

# Bounds (spec `bounds`).
RUN_TOKEN_TTL_S: Final = 1500.0
RUN_TOKENS_MAX: Final = 32
BODY_MAX_BYTES: Final = 33554432
UPSTREAM_TIMEOUT_S: Final = 900.0
PROVIDER_KIND: Final = "anthropic-subscription"
PORT: Final = 8080

# REF-AGENT-4: the headers the bundled CLI sends, minus credentials and connection management.
FORWARD_HEADERS: Final = frozenset(
    {
        "accept",
        "accept-encoding",
        "anthropic-beta",
        "anthropic-dangerous-direct-browser-access",
        "anthropic-version",
        "content-type",
        "user-agent",
        "x-app",
        "x-claude-code-session-id",
    }
)
# Set by the HTTP client or by the proxy itself, never copied from the request.
OWN_HEADERS: Final = frozenset({"host", "content-length", "authorization"})
HOP_BY_HOP: Final = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "content-length",
    }
)

log = logging.getLogger("reader.agent.proxy")
METHODS: Final = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")


def forwarded_header(name: str) -> bool:
    lower = name.lower()
    return lower in FORWARD_HEADERS or lower in OWN_HEADERS or lower.startswith("x-stainless-")


@dataclass(frozen=True)
class ProxySettings:
    config_path: Path = Path("/etc/reader/reader.yaml")  # OPS-CLI's read-only bind target (FR-AGENT-2)
    secrets_root: Path = Path("/run/secrets")
    upstream: str = "https://api.anthropic.com"
    clock: Callable[[], float] = time.monotonic


@dataclass
class Entry:
    handle: str
    token: Secret
    max_output_tokens: int
    model: str
    created: float
    statuses: list[int] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    in_flight: int = 0  # forwarded, not yet fully relayed (code review B2)
    failed: int = 0  # failed after the connection was made

    def record(self) -> dict[str, Any]:
        return {
            "requests": len(self.statuses),
            "statuses": list(self.statuses),
            "any_2xx": any(200 <= s < 300 for s in self.statuses),
            "refused": list(self.refused),
            "in_flight": self.in_flight,
            "failed": self.failed,
        }


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status)


class Proxy:
    def __init__(self, settings: ProxySettings, upstream_transport: httpx.AsyncBaseTransport | None) -> None:
        self.settings = settings
        self.tokens: OrderedDict[str, Entry] = OrderedDict()
        self.client = httpx.AsyncClient(
            transport=upstream_transport,
            timeout=httpx.Timeout(UPSTREAM_TIMEOUT_S),
            follow_redirects=False,
            verify=True,
            trust_env=False,
        )
        self.client.headers.clear()  # no default Accept, User-Agent or Connection of the client library

    # --- token loading (FR-AGENT-2) ---

    def _user_token(self, handle: str) -> Secret | None:
        try:
            config = load_config(self.settings.config_path)
        except ConfigError, OSError:
            return None
        user = next((u for u in config.users if u.handle == handle), None)
        provider = next((p for p in user.providers if p.kind == PROVIDER_KIND), None) if user else None
        if provider is None:
            return None
        try:
            return host_resolved_resolver(self.settings.secrets_root)(SecretRef(provider.token))
        except SecretResolutionError, OSError:
            return None

    # --- run tokens (FR-AGENT-3) ---

    async def issue(self, request: Request) -> Response:
        body = await _json(request, 4096)
        fields = body if isinstance(body, dict) else {}
        handle, cap, model = fields.get("user"), fields.get("max_output_tokens"), fields.get("model")
        if (
            not isinstance(handle, str)
            or type(cap) is not int
            or cap <= 0
            or not isinstance(model, str)
            or not model
        ):
            return _error(400, "bad_request")
        token = self._user_token(handle)
        if token is None:
            return _error(409, "provider_missing")
        run_token = secrets.token_urlsafe(32)
        self.tokens[_digest(run_token)] = Entry(handle, token, cap, model, self.settings.clock())
        while len(self.tokens) > RUN_TOKENS_MAX:
            self.tokens.popitem(last=False)  # the oldest is revoked
        return JSONResponse({"run_token": run_token})

    async def revoke(self, request: Request) -> Response:
        body = await _json(request, 4096)
        run_token = body.get("run_token") if isinstance(body, dict) else None
        if not isinstance(run_token, str):
            return _error(400, "bad_request")
        entry = self.tokens.pop(_digest(run_token), None)
        if entry is None:
            return _error(404, "run_token_unknown")
        return JSONResponse(entry.record())

    # --- forwarding (FR-AGENT-4) ---

    def _live(self, request: Request) -> tuple[str, Entry] | None:
        auth = request.headers.get("authorization", "")
        scheme, _, value = auth.partition(" ")
        if scheme != "Bearer" or not value:
            return None
        digest = _digest(value)
        entry = self.tokens.get(digest)
        if entry is None or self.settings.clock() - entry.created > RUN_TOKEN_TTL_S:
            return None
        return digest, entry

    async def forward(self, request: Request) -> tuple[Response, str]:
        if (
            request.method != "POST"
            or request.url.path != "/v1/messages"
            or request.url.query not in ("", "beta=true")
        ):
            return _error(404, "not_found"), "-"
        live = self._live(request)
        if live is None:
            return _error(401, "run_token_unknown"), "-"
        digest, entry = live
        tag = digest[:8]
        checked = await self._check_body(request, entry)
        if isinstance(checked, Response):
            return checked, tag
        headers = {
            k: v for k, v in request.headers.items() if forwarded_header(k) and k.lower() not in OWN_HEADERS
        }
        headers["authorization"] = f"Bearer {entry.token.reveal()}"
        url = self.settings.upstream + "/v1/messages" + (f"?{request.url.query}" if request.url.query else "")
        upstream_request = self.client.build_request("POST", url, headers=headers, content=checked)
        entry.in_flight += 1
        done = _Once(entry)
        try:
            upstream = await self.client.send(upstream_request, stream=True)
        except httpx.ConnectError, httpx.ConnectTimeout, httpx.UnsupportedProtocol:
            done()  # nothing was written upstream
            return _error(502, "upstream_unavailable"), tag
        except httpx.HTTPError:
            entry.failed += 1  # written, outcome unknown: the settlement takes the reserve
            done()
            return _error(502, "upstream_unavailable"), tag
        entry.statuses.append(upstream.status_code)
        out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP}

        async def close() -> None:
            try:
                await upstream.aclose()
            finally:
                done()

        return (
            StreamingResponse(
                _relay(upstream, done),
                status_code=upstream.status_code,
                headers=out_headers,
                background=BackgroundTask(close),
            ),
            tag,
        )

    async def _check_body(self, request: Request, entry: Entry) -> Response | bytes:
        """The checked body, re-serialised (what was checked is what is sent), or a refusal."""

        def refuse(status: int, code: str) -> Response:
            entry.refused.append(code)
            return _error(status, code)

        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > BODY_MAX_BYTES:
                return refuse(413, "body_too_large")
        try:
            payload = json.loads(body)
        except ValueError, RecursionError:
            return refuse(415, "body_not_json")
        if not isinstance(payload, dict):
            return refuse(415, "body_not_json")
        if any(key in payload and payload[key] not in ([], {}) for key in ("tools", "mcp_servers")):
            return refuse(403, "tools_mismatch")
        if payload.get("model") != entry.model:
            return refuse(403, "model_mismatch")
        max_tokens = payload.get("max_tokens")
        if type(max_tokens) is not int or max_tokens > entry.max_output_tokens:
            return refuse(403, "output_cap")
        try:
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except UnicodeEncodeError:  # a lone surrogate (`\ud800`): a 4xx, which the CLI does not retry
            return refuse(400, "body_not_utf8")


class _Once:
    """Marks one forwarded request finished, exactly once, whichever path ends it.

    A client that disconnects before the relay starts leaves `in_flight` up on purpose: neither the relay
    nor the background close then runs, the request may still have been billed, and the run settles
    conservatively at the reserve (FR-AGENT-9, `outcome_unknown`).
    """

    def __init__(self, entry: Entry) -> None:
        self.entry: Entry | None = entry

    def __call__(self) -> None:
        if self.entry is not None:
            self.entry.in_flight -= 1
            self.entry = None


async def _relay(upstream: httpx.Response, done: _Once) -> AsyncIterator[bytes]:
    try:
        if upstream.is_stream_consumed:  # a transport that hands over a body already read (in-process)
            yield upstream.content
            return
        async for chunk in upstream.aiter_raw():
            yield chunk
    finally:
        done()


async def _json(request: Request, limit: int) -> Any:
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return None
    try:
        return json.loads(body)
    except ValueError, RecursionError:
        return None


def create_app(
    settings: ProxySettings | None = None, *, upstream_transport: httpx.AsyncBaseTransport | None = None
) -> Starlette:
    proxy = Proxy(settings or ProxySettings(), upstream_transport)
    clock = time.monotonic
    # httpx logs every request URL at INFO, query string included (FR-AGENT-5 forbids it).
    logging.getLogger("httpx").setLevel(logging.WARNING)

    def logged(method: str, route: str, status: int, started: float, tag: str) -> None:
        # FR-AGENT-5: the route template only; never a raw path, user, header value, body, query or token.
        log.info("%s %s %s %dms token=%s", method, route, status, int((clock() - started) * 1000), tag)

    async def run_tokens(request: Request) -> Response:
        started = clock()
        response = await proxy.issue(request)
        logged("POST", "/run-tokens", response.status_code, started, "-")
        return response

    async def revoke(request: Request) -> Response:
        started = clock()
        response = await proxy.revoke(request)
        logged("POST", "/run-tokens/revoke", response.status_code, started, "-")
        return response

    async def forward(request: Request) -> Response:
        started = clock()
        response, tag = await proxy.forward(request)
        route = "/v1/messages" if request.url.path == "/v1/messages" else "other"
        method = request.method if request.method in METHODS else "OTHER"
        logged(method, route, response.status_code, started, tag)
        return response

    app = Starlette(
        routes=[
            Route("/run-tokens", run_tokens, methods=["POST"]),
            Route("/run-tokens/revoke", revoke, methods=["POST"]),
            Route("/{path:path}", forward, methods=list(METHODS)),
        ]
    )
    app.state.proxy = proxy
    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(name)s %(levelname)s %(message)s")
    uvicorn.run(create_app(), host="0.0.0.0", port=PORT, access_log=False, log_config=None)  # noqa: S104


if __name__ == "__main__":
    main()
