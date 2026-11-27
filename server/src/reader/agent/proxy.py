"""agent-proxy: the credential-injecting reverse proxy of ADR-0025 (SRV-AGENT FR-AGENT-1..5).

It holds provider tokens; the worker gets a short-lived run_token bound to one user and one output cap, and
the CLI sends it as its bearer. agent-proxy swaps it for that user's token and forwards exactly
`POST /v1/messages` (query empty or `beta=true`) to one pinned upstream, with an allow-list of request headers
and two body checks. It records every upstream status per run_token and returns that record on revoke.
Entry point: `python -I -m reader.agent.proxy`.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import logging
import secrets
import sys
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
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
RUN_TOKENS_MAX: Final = 32  # = the sum of the client quotas below
LANE_TOKEN_TTL_S: Final = 30.0
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

# FR-AGENT-15 (ADR-0030): the one task whose run_tokens may carry a WebSearch allowance, and the pinned
# shapes of the CLI's WebSearch requests (VEC-AGENT-3; claude-agent-sdk 0.2.187). Any other version or pin
# needs a new review.
TOOL_TASK: Final = "freshness_web"
WEB_SEARCH_VERSION: Final = "web_search_20250305"
WEBSEARCH_INPUT_SCHEMA_SHA256: Final = "ce035ada7312377e4219f1f4d74d325e1db5b9e3b9c4bf9b8e46e7e119b7cbbb"
PLACEHOLDER_INPUT_SCHEMA_SHA256: Final = "efddc7bd8bbcef73a14eb1ace1ffdaec81e518ef1e13c1e9271d0b8acb694a49"
WEBSEARCH_DESCRIPTION_MAX: Final = 1000
PLACEHOLDER_DESCRIPTION_MAX: Final = 200
ALLOWANCE_KEYS: Final = ("searches_max", "searches_per_request_max", "requests_max")


def canonical_sha256(value: Any) -> str:
    """sha256 of canonical JSON: sorted keys, no spaces, UTF-8 without ASCII escaping."""
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _is_count(value: Any) -> bool:
    return type(value) is int and value >= 1


def _main_tools_ok(tools: list[Any]) -> bool:
    """Shape (a): exactly one WebSearch client tool, plus at most one DeferredToolPlaceholder."""
    searches = [t for t in tools if isinstance(t, dict) and t.get("name") == "WebSearch"]
    holders = [t for t in tools if isinstance(t, dict) and t.get("name") == "DeferredToolPlaceholder"]
    if len(searches) != 1 or len(holders) > 1 or len(searches) + len(holders) != len(tools):
        return False
    ws = searches[0]
    if set(ws) != {"name", "description", "input_schema"}:
        return False
    if not isinstance(ws["description"], str) or len(ws["description"]) > WEBSEARCH_DESCRIPTION_MAX:
        return False
    if canonical_sha256(ws["input_schema"]) != WEBSEARCH_INPUT_SCHEMA_SHA256:
        return False
    for holder in holders:
        if set(holder) != {"name", "description", "input_schema", "defer_loading"}:
            return False
        if holder["defer_loading"] is not True or not isinstance(holder["description"], str):
            return False
        if len(holder["description"]) > PLACEHOLDER_DESCRIPTION_MAX:
            return False
        if canonical_sha256(holder["input_schema"]) != PLACEHOLDER_INPUT_SCHEMA_SHA256:
            return False
    return True


def _nested_tools_ok(tools: list[Any]) -> bool:
    """Shape (b): exactly the pinned server tool, with at most `max_uses` beside `type` and `name`."""
    if len(tools) != 1 or not isinstance(tools[0], dict):
        return False
    tool = tools[0]
    if not set(tool) <= {"type", "name", "max_uses"}:
        return False
    if tool.get("type") != WEB_SEARCH_VERSION or tool.get("name") != "web_search":
        return False
    return "max_uses" not in tool or _is_count(tool["max_uses"])


def forwarded_header(name: str) -> bool:
    lower = name.lower()
    return lower in FORWARD_HEADERS or lower in OWN_HEADERS or lower.startswith("x-stainless-")


@dataclass(frozen=True)
class Client:
    """A caller of agent-proxy, named by its fixed agentnet address (FR-AGENT-3, ADR-0039 B7b)."""

    name: str
    quota: int  # live tokens at most
    ttl_s: float
    tools: bool  # may be issued a web_search allowance (FR-AGENT-15)
    evict: bool  # at its quota: revoke its own oldest token (True) or refuse the issue with 429 (False)


# The fixed addresses OPS-DEPLOY FR-DEPLOY-16 declares on agentnet (INV-DEPLOY-11 pins them equal).
CLIENTS: Final[Mapping[str, Client]] = MappingProxyType(
    {
        "10.230.7.10": Client("worker", 28, RUN_TOKEN_TTL_S, tools=True, evict=True),
        "10.230.7.11": Client("worker-judge", 4, LANE_TOKEN_TTL_S, tools=False, evict=False),
    }
)


@dataclass(frozen=True)
class ProxySettings:
    config_path: Path = Path("/etc/reader/reader.yaml")  # OPS-CLI's read-only bind target (FR-AGENT-2)
    secrets_root: Path = Path("/run/secrets")
    upstream: str = "https://api.anthropic.com"
    clock: Callable[[], float] = time.monotonic
    clients: Mapping[str, Client] = field(default_factory=lambda: CLIENTS)  # source address → client


@dataclass
class Entry:
    handle: str
    token: Secret
    max_output_tokens: int
    model: str
    created: float
    client: str  # the issuing client's name (FR-AGENT-3)
    ttl_s: float
    statuses: list[int] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    in_flight: int = 0  # forwarded, not yet fully relayed (code review B2)
    failed: int = 0  # failed after the connection was made
    allowance: dict[str, int] | None = None  # FR-AGENT-15: a freshness_web token's web_search allowance
    task: str | None = None
    forwarded: int = 0  # requests forwarded (FR-AGENT-16's request cap counts these)
    searches_forwarded: int = 0  # the sum of forwarded max_uses

    def record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "requests": len(self.statuses),
            "statuses": list(self.statuses),
            "any_2xx": any(200 <= s < 300 for s in self.statuses),
            "refused": list(self.refused),
            "in_flight": self.in_flight,
            "failed": self.failed,
        }
        if self.task == TOOL_TASK:  # FR-AGENT-16, with or without an allowance
            record["searches_forwarded"] = self.searches_forwarded
        return record


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

    # --- clients (FR-AGENT-3, ADR-0039 B7b) ---

    def client_of(self, request: Request) -> Client | None:
        """The calling client, from the connection's source address only (never a header or a body field)."""
        host = request.client.host if request.client is not None else None
        return self.settings.clients.get(host) if host is not None else None

    def _purge(self) -> None:
        now = self.settings.clock()
        for digest in [d for d, e in self.tokens.items() if now - e.created > e.ttl_s]:
            del self.tokens[digest]

    def live(self, client: str) -> int:
        """Live tokens of one client (expired entries purged first)."""
        self._purge()
        return sum(1 for e in self.tokens.values() if e.client == client)

    # --- run tokens (FR-AGENT-3) ---

    async def issue(self, request: Request, client: Client) -> Response:
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
        task, web_search = fields.get("task"), fields.get("web_search")
        if task is not None and not isinstance(task, str):
            return _error(400, "bad_request")
        allowance: dict[str, int] | None = None
        if "web_search" in fields:  # FR-AGENT-15: only a freshness_web token may carry an allowance
            if task != TOOL_TASK or not client.tools:  # the lane never has tools, whatever its task
                return _error(400, "tools_not_allowed")
            if (
                not isinstance(web_search, dict)
                or set(web_search) != set(ALLOWANCE_KEYS)
                or not all(_is_count(web_search[k]) for k in ALLOWANCE_KEYS)
            ):
                return _error(400, "bad_request")
            allowance = {k: int(web_search[k]) for k in ALLOWANCE_KEYS}
        token = self._user_token(handle)
        if token is None:
            return _error(409, "provider_missing")
        # Purge, quota count and insert with no `await` between them: two issues cannot both pass a quota.
        self._purge()
        own = [d for d, e in self.tokens.items() if e.client == client.name]
        if len(own) >= client.quota:
            if not client.evict:
                return _error(429, "client_quota")
            for digest in own[: len(own) - client.quota + 1]:
                del self.tokens[digest]  # the client's own oldest; never another client's
        run_token = secrets.token_urlsafe(32)
        self.tokens[_digest(run_token)] = Entry(
            handle,
            token,
            cap,
            model,
            self.settings.clock(),
            client.name,
            client.ttl_s,
            allowance=allowance,
            task=task,
        )
        return JSONResponse({"run_token": run_token})

    async def revoke(self, request: Request, client: Client) -> Response:
        body = await _json(request, 4096)
        run_token = body.get("run_token") if isinstance(body, dict) else None
        if not isinstance(run_token, str):
            return _error(400, "bad_request")
        self._purge()
        digest = _digest(run_token)
        entry = self.tokens.get(digest)
        if entry is None or entry.client != client.name:  # another client's token stays live
            return _error(404, "run_token_unknown")
        del self.tokens[digest]
        return JSONResponse(entry.record())

    def revoke_all(self, client: Client) -> Response:
        own = [d for d, e in self.tokens.items() if e.client == client.name]
        for digest in own:
            del self.tokens[digest]
        return JSONResponse({"revoked": len(own)})

    # --- forwarding (FR-AGENT-4) ---

    def _live(self, request: Request, client: Client) -> tuple[str, Entry] | None:
        auth = request.headers.get("authorization", "")
        scheme, _, value = auth.partition(" ")
        if scheme != "Bearer" or not value:
            return None
        self._purge()
        digest = _digest(value)
        entry = self.tokens.get(digest)
        if entry is None or entry.client != client.name:  # only the calling client's own live token
            return None
        return digest, entry

    async def forward(self, request: Request, client: Client) -> tuple[Response, str]:
        if (
            request.method != "POST"
            or request.url.path != "/v1/messages"
            or request.url.query not in ("", "beta=true")
        ):
            return _error(404, "not_found"), "-"
        live = self._live(request, client)
        if live is None:
            return _error(401, "run_token_unknown"), "-"
        digest, entry = live
        tag = digest[:8]
        outcome = await self._check_body(request, entry)
        if isinstance(outcome, Response):
            return outcome, tag
        checked, kind = outcome
        if kind is not None:
            tag = f"{tag} kind={kind}"  # FR-AGENT-16: the route kind of an allowance token, nothing else
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

    async def _check_body(self, request: Request, entry: Entry) -> Response | tuple[bytes, str | None]:
        """The checked body, re-serialised (what was checked is what is sent), with the route kind of an
        allowance token (`main` | `search`, else None), or a refusal."""

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
        kind: str | None = None
        if "mcp_servers" in payload and payload["mcp_servers"] not in ([], {}):
            return refuse(403, "tools_mismatch")
        if entry.allowance is None:
            if "tools" in payload and payload["tools"] not in ([], {}):
                return refuse(403, "tools_mismatch")
        else:
            kind = self._shape(payload)
            if kind is None:
                return refuse(403, "tools_mismatch")
        if payload.get("model") != entry.model:
            return refuse(403, "model_mismatch")
        max_tokens = payload.get("max_tokens")
        if type(max_tokens) is not int or max_tokens > entry.max_output_tokens:
            return refuse(403, "output_cap")
        if entry.allowance is not None:
            if entry.forwarded >= entry.allowance["requests_max"]:
                return refuse(403, "request_cap")
            if kind == "search":
                remaining = entry.allowance["searches_max"] - entry.searches_forwarded
                if remaining <= 0:
                    return refuse(403, "search_cap")
                per_request = entry.allowance["searches_per_request_max"]
                tool = payload["tools"][0]
                # FR-AGENT-16: the only field the proxy ever rewrites.
                tool["max_uses"] = min(tool.get("max_uses", per_request), per_request, remaining)
        try:
            body_out = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except UnicodeEncodeError:  # a lone surrogate (`\ud800`): a 4xx, which the CLI does not retry
            return refuse(400, "body_not_utf8")
        if entry.allowance is not None:  # counted when it is certain to be sent
            entry.forwarded += 1
            if kind == "search":
                entry.searches_forwarded += payload["tools"][0]["max_uses"]
        return body_out, kind

    @staticmethod
    def _shape(payload: dict[str, Any]) -> str | None:
        """FR-AGENT-15: `main` or `search` for one of the two pinned shapes, else None."""
        tools = payload.get("tools")
        if not isinstance(tools, list) or not tools:
            return None
        if payload.get("tool_choice", {"type": "auto"}) != {"type": "auto"}:
            return None
        if any(isinstance(t, dict) and "type" in t for t in tools):
            return "search" if _nested_tools_ok(tools) else None
        return "main" if _main_tools_ok(tools) else None


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
        client = proxy.client_of(request)
        response = _error(403, "client_unknown") if client is None else await proxy.issue(request, client)
        logged("POST", "/run-tokens", response.status_code, started, "-")
        return response

    async def revoke(request: Request) -> Response:
        started = clock()
        client = proxy.client_of(request)
        response = _error(403, "client_unknown") if client is None else await proxy.revoke(request, client)
        logged("POST", "/run-tokens/revoke", response.status_code, started, "-")
        return response

    async def revoke_all(request: Request) -> Response:
        started = clock()
        client = proxy.client_of(request)
        response = _error(403, "client_unknown") if client is None else proxy.revoke_all(client)
        logged("POST", "/run-tokens/revoke-all", response.status_code, started, "-")
        return response

    async def forward(request: Request) -> Response:
        started = clock()
        client = proxy.client_of(request)
        if client is None:  # nothing an unknown address sends is forwarded (FR-AGENT-3)
            response: Response = _error(403, "client_unknown")
            tag = "-"
        else:
            response, tag = await proxy.forward(request, client)
        route = "/v1/messages" if request.url.path == "/v1/messages" else "other"
        method = request.method if request.method in METHODS else "OTHER"
        logged(method, route, response.status_code, started, tag)
        return response

    app = Starlette(
        routes=[
            Route("/run-tokens", run_tokens, methods=["POST"]),
            Route("/run-tokens/revoke", revoke, methods=["POST"]),
            Route("/run-tokens/revoke-all", revoke_all, methods=["POST"]),
            Route("/{path:path}", forward, methods=list(METHODS)),
        ]
    )
    app.state.proxy = proxy
    return app


def _bind(value: str) -> tuple[str, int]:
    """`<ipv4>:<port>` (FR-AGENT-3: OPS-DEPLOY passes the agentnet address); else a usage error."""
    host, sep, port = value.rpartition(":")
    try:
        address = ipaddress.IPv4Address(host)
        number = int(port)
    except ValueError:
        raise argparse.ArgumentTypeError("expected <ipv4>:<port>") from None
    if not sep or not port.isdigit() or not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("expected <ipv4>:<port>")
    return str(address), number


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    parser = argparse.ArgumentParser(prog="reader.agent.proxy")
    parser.add_argument("--bind", type=_bind, default=("0.0.0.0", PORT))  # noqa: S104 - the loopback harness default
    host, port = parser.parse_args(sys.argv[1:] if argv is None else argv).bind
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(name)s %(levelname)s %(message)s")
    # proxy_headers=False: X-Forwarded-For is never read; the client is the TCP source address (FR-AGENT-3).
    uvicorn.run(create_app(), host=host, port=port, access_log=False, log_config=None, proxy_headers=False)


if __name__ == "__main__":
    main()
