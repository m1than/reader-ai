"""Fixtures and helpers for the SRV-AGENT tests (spec/modules/SRV-AGENT.yaml).

@trace FR-AGENT-9

Modules under test are imported through `need()` inside test bodies, so a missing module is an assertion failure in
the Red phase. [db] tests use the Postgres image OPS-DEPLOY pins through testcontainers and skip without Docker.
The upstream API is an `httpx.MockTransport` that records every request it receives (no network); the CLI is a fake
that yields objects shaped like the Agent SDK's messages (class names and fields as measured in
spikes/agent-proxy-handshake).
"""

from __future__ import annotations

import importlib
import json
import secrets
import shutil
import subprocess
import textwrap
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from ruamel.yaml import YAML

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "spec" / "fixtures" / "SRV-AGENT"
SPEC = REPO / "spec" / "modules" / "SRV-AGENT.yaml"
# postgres:18-trixie plus pgvector 0.8.7: the heads include SRV-SEARCH's migration (CREATE EXTENSION vector).
PG_IMAGE = (
    "pgvector/pgvector:0.8.7-pg18-trixie@sha256:ab49524d18df7200b74ac9a475e1db6297fc1eb57da4c214c63d10ecc4b754fa"
)


def load_yaml(path: Path) -> Any:
    return YAML(typ="safe").load(path.read_text(encoding="utf-8"))


def spec_bounds() -> dict[str, Any]:
    data: dict[str, Any] = load_yaml(SPEC)["bounds"]
    return data


def need(name: str) -> ModuleType:
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as error:
        raise AssertionError(f"{name} is not implemented ({error.name} missing)") from None


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# --- Postgres -----------------------------------------------------------------------------------------------------


def _daemon_up() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0


DOCKER = _daemon_up()


@dataclass(frozen=True)
class Pg:
    host: str
    port: int
    user: str
    password: str
    database: str


@pytest.fixture(scope="session")
def pg_container() -> Iterator[tuple[Pg, Any]]:
    if not DOCKER:
        pytest.skip("docker daemon not reachable (Postgres via testcontainers)")
    from testcontainers.community.postgres import PostgresContainer

    password = "pw-" + secrets.token_hex(8)
    with PostgresContainer(
        PG_IMAGE, username="postgres", password=password, dbname="postgres", driver=None
    ) as c:
        yield (
            Pg(c.get_container_host_ip(), int(c.get_exposed_port(5432)), "postgres", password, "postgres"),
            c,
        )


@pytest.fixture
def pg(pg_container: tuple[Pg, Any]) -> Pg:
    server, container = pg_container
    name = "a_" + secrets.token_hex(6)
    code, out = container.exec(["psql", "-U", "postgres", "-c", f"CREATE DATABASE {name}"])
    assert code == 0, out
    return Pg(server.host, server.port, server.user, server.password, name)


CONFIG = """\
version: 1
instance: { url: "https://localhost", data_dir: ./data }
users:
  - handle: ivan
    role: owner
    providers:
      - { kind: anthropic-subscription, token: "secret://keychain/reader-ai/claude-oauth-token" }
  - handle: anna
    role: learner
    providers:
      - { kind: anthropic-subscription, token: "secret://keychain/reader-ai/anna-token" }
  - { handle: lea, role: learner }
tasks:
  draft:      { model: opus,   budget_usd_per_lesson: 4.0 }
  fact_check: { model: sonnet, budget_usd_per_lesson: 2.0 }
  review:     { model: opus,   budget_usd_per_lesson: 4.0 }
budgets: { monthly_usd: 200, warn_at: 0.8 }
sharing: {}
observability: { enabled: false }
"""
# FR-AGENT-14 (paired SRV-OBS change): tracing is off here, so FR-AGENT-6's environment stays exact for these tests;
# test_obs_env.py turns it on.


def write_config(tmp: Path, text: str = CONFIG) -> Path:
    path = tmp / "reader.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


@dataclass
class World:
    engine: Any
    config: Any
    users: dict[str, uuid.UUID]


async def world(pg: Pg, tmp: Path) -> World:
    """A database migrated to all heads, with the users of CONFIG."""
    from sqlalchemy import text

    from reader.auth.policy import settings_from_config
    from reader.auth.store import prepare
    from reader.config import Secret, load_config
    from reader.db import DbTarget, make_engine, migrate

    config = load_config(write_config(tmp))
    engine = make_engine(DbTarget(pg.host, pg.port, pg.user, pg.database, Secret(pg.password)))
    await migrate(engine)
    await prepare(engine, settings_from_config(config))
    async with engine.connect() as conn:
        users = {r[0]: r[1] for r in await conn.execute(text("SELECT handle, id FROM users"))}
    return World(engine, config, users)


async def scalar(engine: Any, sql: str, **params: Any) -> Any:
    from sqlalchemy import text

    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar()


async def rows(engine: Any, sql: str, **params: Any) -> list[Any]:
    from sqlalchemy import text

    async with engine.connect() as conn:
        return list((await conn.execute(text(sql), params)).all())


# --- the upstream stub (httpx.MockTransport) ----------------------------------------------------------------------

SSE_OK = (
    b'event: message_start\ndata: {"type":"message_start"}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


@dataclass
class Upstream:
    """A fake api.anthropic.com: records requests; answers `status` (200 with an SSE body by default)."""

    status: int = 200
    requests: list[httpx.Request] = field(default_factory=list)
    bodies: list[bytes] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(request.read())
        if self.status == 200:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream", "connection": "keep-alive", "x-upstream": "1"},
                content=SSE_OK,
            )
        return httpx.Response(
            self.status, json={"type": "error", "error": {"type": "api_error", "message": "x"}}
        )

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def message_body(**extra: Any) -> bytes:
    body: dict[str, Any] = {"model": "claude-sonnet-5-5", "max_tokens": 16000, "messages": [], "tools": []}
    body.update(extra)
    for key in [k for k, v in extra.items() if v is _DROP]:
        body.pop(key)
    return json.dumps(body).encode()


_DROP = object()
DROP: Any = _DROP


@dataclass
class Proxy:
    app: Any
    upstream: Upstream
    secrets_root: Path
    clock: list[float]

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://agent-proxy:8080"
        )


TOKENS = {"ivan": "sk-ant-oat01-IVAN-" + "i" * 40, "anna": "sk-ant-oat01-ANNA-" + "a" * 40}
REFS = {
    "ivan": "secret://keychain/reader-ai/claude-oauth-token",
    "anna": "secret://keychain/reader-ai/anna-token",
}


def deliver_tokens(root: Path, handles: tuple[str, ...] = ("ivan", "anna")) -> None:
    """What OPS-CLI does: one file per provider reference, named secret_file_name(ref), mode 0600."""
    from reader.config import secret_file_name

    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    for handle in handles:
        path = root / secret_file_name(REFS[handle])
        path.write_text(TOKENS[handle], encoding="utf-8")
        path.chmod(0o600)


def make_proxy(
    tmp: Path, upstream: Upstream | None = None, handles: tuple[str, ...] = ("ivan", "anna")
) -> Proxy:
    proxy = need("reader.agent.proxy")
    up = upstream or Upstream()
    root = tmp / "secrets"
    deliver_tokens(root, handles)
    clock = [1000.0]
    settings = proxy.ProxySettings(config_path=write_config(tmp), secrets_root=root, clock=lambda: clock[0])
    return Proxy(proxy.create_app(settings, upstream_transport=up.transport()), up, root, clock)


async def issue(
    c: httpx.AsyncClient,
    handle: str = "ivan",
    max_output_tokens: int = 16000,
    model: str = "claude-sonnet-5-5",
) -> str:
    r = await c.post(
        "/run-tokens", json={"user": handle, "max_output_tokens": max_output_tokens, "model": model}
    )
    assert r.status_code == 200, r.text
    token: str = r.json()["run_token"]
    return token


async def forward(
    c: httpx.AsyncClient, token: str | None, body: bytes | None = None, **kw: Any
) -> httpx.Response:
    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    headers.update(kw.pop("headers", {}))
    path = kw.pop("path", "/v1/messages?beta=true")
    method = kw.pop("method", "POST")
    return await c.request(method, path, content=message_body() if body is None else body, headers=headers)


# --- a fake Agent SDK CLI -----------------------------------------------------------------------------------------


@dataclass
class SystemMessage:
    subtype: str
    data: dict[str, Any]


@dataclass
class TextBlock:
    text: str


@dataclass
class AssistantMessage:
    content: list[Any]
    model: str
    error: str | None = None


@dataclass
class ResultMessage:
    subtype: str
    is_error: bool
    total_cost_usd: float | None
    usage: dict[str, Any] | None
    result: str | None
    stop_reason: str | None = None


def sdk_usage(output: int = 100, write_1h: int = 7000) -> dict[str, Any]:
    return {
        "input_tokens": 2,
        "output_tokens": output,
        "cache_creation_input_tokens": write_1h,
        "cache_read_input_tokens": 0,
        "cache_creation": {"ephemeral_1h_input_tokens": write_1h, "ephemeral_5m_input_tokens": 0},
    }


@dataclass
class Script:
    """What the fake CLI emits for one call, and what agent-proxy saw (the run_token's record)."""

    text: str = "ok"
    cost: float | None = 0.05
    usage: dict[str, Any] | None = field(default_factory=sdk_usage)
    init_tools: list[str] | None = field(default_factory=list)
    init_mcp: list[Any] = field(default_factory=list)
    init_model: str | None = None  # None: the requested model
    assistant_error: str | None = None
    is_error: bool = False
    statuses: tuple[int, ...] = (200,)
    refused: tuple[str, ...] = ()
    raise_after: bool = False  # the SDK raises after the ResultMessage (as it does for is_error)
    revoke_unknown: bool = False
    stop_reason: str | None = "end_turn"
    api_retries: int = 0  # SystemMessage subtype api_retry before the answer (FR-AGENT-13 d)
    sleep_s: float = 0.0  # before the answer (FR-AGENT-13 e)
    send_init: bool = True  # False: the init SystemMessage never arrives (FR-AGENT-7)


@dataclass
class FakeCli:
    scripts: list[Script]
    requests: list[Any] = field(default_factory=list)
    closed: list[bool] = field(default_factory=list)
    systems: list[tuple[str, int] | None] = field(default_factory=list)  # (text, mode) of the system file

    async def __call__(self, request: Any) -> AsyncIterator[Any]:
        import asyncio

        script = self.scripts.pop(0)
        self.requests.append(request)
        path = request.system_file
        self.systems.append((path.read_text(encoding="utf-8"), path.stat().st_mode & 0o777) if path else None)
        self.current = script
        try:
            if script.send_init:
                yield SystemMessage(
                    "init",
                    {
                        "tools": script.init_tools,
                        "mcp_servers": script.init_mcp,
                        "model": script.init_model or request.model,
                    },
                )
            for attempt in range(script.api_retries):
                yield SystemMessage("api_retry", {"attempt": attempt + 1, "error_status": 429})
            if script.sleep_s:
                await asyncio.sleep(script.sleep_s)
            yield AssistantMessage([TextBlock(script.text)], request.model, script.assistant_error)
            yield ResultMessage(
                "success",
                script.is_error,
                script.cost,
                script.usage,
                script.text if not script.is_error else "API Error",
                script.stop_reason,
            )
        except GeneratorExit, asyncio.CancelledError:
            self.closed.append(True)  # the adapter closed the CLI early
            raise
        if script.raise_after or script.is_error:
            raise RuntimeError("Claude Code returned an error result")


@dataclass
class FakeProxyClient:
    """The worker's view of agent-proxy: issue and revoke (FR-AGENT-3)."""

    cli: FakeCli
    issued: list[tuple[str, int, str]] = field(default_factory=list)
    revoked: list[str] = field(default_factory=list)
    missing: set[str] = field(default_factory=set)

    async def issue(self, handle: str, max_output_tokens: int, model: str) -> str:
        if handle in self.missing:
            errors = need("reader.agent.errors")
            raise errors.agent_error("provider_missing")
        self.issued.append((handle, max_output_tokens, model))
        return "rt-" + secrets.token_urlsafe(8)

    async def revoke(self, run_token: str) -> Any:
        self.revoked.append(run_token)
        script: Script = self.cli.current
        if script.revoke_unknown:
            return None
        ledger = need("reader.agent.ledger")
        return ledger.UpstreamRecord(statuses=script.statuses, refused=script.refused)
