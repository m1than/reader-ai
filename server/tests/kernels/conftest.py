"""Helpers for the SRV-KERNEL tests (spec/modules/SRV-KERNEL.yaml).

[unit] tests drive `reader.kernels` against in-memory fakes: `FakeUpstream` stands in for Kernel Gateway's
REST API, the supervisor's control endpoint and the kernel channels WebSocket; `FakeBrowser` for a browser
connection; `FakeClock` for time. The real HTTP client and WebSocket connect (`reader.kernels.upstream`) are
tested against local servers in test_upstream.py; route tests use Postgres (as SRV-AUTH's tests do); [image]
tests use `reader-kernel:local`.

The modules under test are imported inside test bodies through `load`, which asserts that the module exists,
so in the Red phase every test fails on an assertion of its own.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import secrets
import shutil
import subprocess
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "spec" / "fixtures" / "SRV-KERNEL"
PG_IMAGE = "postgres:18-trixie@sha256:eb63372c1e05c755f55fa30cbd07aecc443676fc6c0fa90b9002d01537841e1a"
ORIGIN = "https://localhost"


def load(name: str) -> Any:
    """Import a module under test; a missing module is an assertion failure in the calling test."""
    assert importlib.util.find_spec(name) is not None, f"{name} does not exist yet"
    return importlib.import_module(name)


def fixture(name: str) -> Any:
    from ruamel.yaml import YAML

    return YAML(typ="safe").load((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# Time


@dataclass
class FakeClock:
    now: float = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def advancing_sleep(clock: FakeClock) -> Callable[[float], Any]:
    """A sleep that moves the fake clock by the requested time (reset polling tests)."""

    async def sleep(seconds: float) -> None:
        clock.advance(seconds)
        await asyncio.sleep(0)

    return sleep


async def tick_sleep(_: float) -> None:
    """A sleep that only yields (relay timer tests advance the clock themselves)."""
    await asyncio.sleep(0.002)


# Upstream fake


class FakeWs:
    """An in-memory kernel channels WebSocket (the relay's view of `websockets.ClientConnection`)."""

    def __init__(self, kernel_id: str) -> None:
        self.kernel_id = kernel_id
        self.sent: list[dict[str, Any]] = []
        self.inbox: asyncio.Queue[Any] = asyncio.Queue()
        self.closed = False

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))

    def push(self, frame: Any) -> None:
        """Deliver a frame from the kernel: a dict (JSON-encoded), str, bytes, or an exception to raise."""
        self.inbox.put_nowait(json.dumps(frame) if isinstance(frame, dict) else frame)

    def __aiter__(self) -> FakeWs:
        return self

    async def __anext__(self) -> Any:
        item = await self.inbox.get()
        if isinstance(item, BaseException):
            raise item
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self) -> None:
        self.closed = True
        self.inbox.put_nowait(None)


def closed_with(sent_code: int | None, rcvd_code: int | None = None) -> BaseException:
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close

    return ConnectionClosedError(
        Close(rcvd_code, "") if rcvd_code is not None else None,
        Close(sent_code, "") if sent_code is not None else None,
    )


@dataclass
class Call:
    method: str
    host: str
    port: int
    path: str
    body: Any


@dataclass
class FakeUpstream:
    """Kernel Gateway REST, the control endpoint and the channels WebSocket for any number of handles.

    `states` scripts successive `/control/state` answers per host (a dict, or "refused"); when it runs out,
    the current state (`nonce`, `gateway`) is answered. `/control/exit` starts a new container: the next
    answers come from the script, else a new nonce with `up`.
    """

    nonce: dict[str, str] = field(default_factory=dict)
    gateway: dict[str, str] = field(default_factory=dict)
    states: dict[str, list[Any]] = field(default_factory=dict)
    exit_status: int = 202
    create_reply: tuple[int, Any] | None = None
    fail: set[str] = field(default_factory=set)   # paths that raise UpstreamError
    unresolvable: set[str] = field(default_factory=set)
    status_for: dict[str, int] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    kernels: dict[str, set[str]] = field(default_factory=dict)   # host -> kernel ids
    sockets: dict[str, FakeWs] = field(default_factory=dict)
    ws_fail: bool = False
    ws_hosts: list[str] = field(default_factory=list)

    def endpoint(self, handle: str) -> Any:
        upstream = load("reader.kernels.upstream")
        return upstream.Endpoint(f"kernel-user-{handle}", 8888, 8889)

    def _state_of(self, host: str) -> dict[str, str]:
        self.nonce.setdefault(host, secrets.token_hex(16))
        self.gateway.setdefault(host, "up")
        return {"nonce": self.nonce[host], "gateway": self.gateway[host]}

    async def http(self, method: str, host: str, port: int, path: str, body: Any = None) -> Any:
        upstream = load("reader.kernels.upstream")
        self.calls.append(Call(method, host, port, path, body))
        await asyncio.sleep(0)
        if host in self.unresolvable:
            raise upstream.UpstreamError("gaierror")
        if path in self.fail:
            raise upstream.UpstreamError("fail")
        reply = upstream.Reply
        if port == 8889 and path == "/control/state":
            script = self.states.get(host)
            if script:
                item = script.pop(0)
                if item == "refused":
                    raise upstream.UpstreamError("ConnectionRefusedError")
                self.nonce[host], self.gateway[host] = item["nonce"], item["gateway"]
            return reply(200, json.dumps(self._state_of(host)).encode())
        if port == 8889 and path == "/control/exit":
            if self.exit_status == 202 and host not in self.states:
                self.nonce[host] = secrets.token_hex(16)
                self.gateway[host] = "up"
                self.kernels[host] = set()
            return reply(self.exit_status, b"")
        if path in self.status_for:
            return reply(self.status_for[path], b"")
        if method == "POST" and path == "/api/kernels":
            if self.create_reply is not None:
                status, payload = self.create_reply
                return reply(status, json.dumps(payload).encode())
            kid = str(uuid.uuid4())
            self.kernels.setdefault(host, set()).add(kid)
            return reply(201, json.dumps({"id": kid, "name": "python3"}).encode())
        if method == "DELETE" and path.startswith("/api/kernels/"):
            kid = path.rsplit("/", 1)[1]
            if kid in self.kernels.get(host, set()):
                self.kernels[host].discard(kid)
                return reply(204, b"")
            return reply(404, b"")
        if method == "POST" and path.endswith("/interrupt"):
            kid = path.split("/")[3]
            return reply(204 if kid in self.kernels.get(host, set()) else 404, b"")
        return reply(404, b"")

    async def ws(self, endpoint: Any, kernel_id: str) -> FakeWs:
        upstream = load("reader.kernels.upstream")
        self.ws_hosts.append(endpoint.host)
        if self.ws_fail:
            raise upstream.UpstreamError("ConnectionRefusedError")
        sock = FakeWs(kernel_id)
        self.sockets[kernel_id] = sock
        return sock

    def requests_to(self, path_suffix: str) -> list[Call]:
        return [c for c in self.calls if c.path.endswith(path_suffix)]


def make_service(up: FakeUpstream, clock: FakeClock | None = None, sleep: Any = None) -> Any:
    service = load("reader.kernels.service")
    clock = clock or FakeClock()
    return service.KernelService(
        up.endpoint, clock=clock, sleep=sleep or advancing_sleep(clock), http=up.http, ws=up.ws
    )


# Browser fake


class FakeBrowser:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str | bytes | None] = asyncio.Queue()
        self.received: list[dict[str, Any]] = []
        self.close_code: int | None = None
        self.closed = asyncio.Event()

    def send(self, frame: str | bytes | dict[str, Any]) -> None:
        self.incoming.put_nowait(json.dumps(frame) if isinstance(frame, dict) else frame)

    def disconnect(self) -> None:
        self.incoming.put_nowait(None)

    async def receive(self) -> str | bytes | None:
        return await self.incoming.get()

    async def send_text(self, text: str) -> None:
        self.received.append(json.loads(text))

    async def close(self, code: int) -> None:
        self.close_code = code
        self.closed.set()
        self.incoming.put_nowait(None)

    def notices(self) -> list[tuple[str, str | None]]:
        return [
            (m["content"]["code"], m["content"]["parent_msg_id"])
            for m in self.received
            if m.get("channel") == "reader"
        ]

    def types(self) -> list[str]:
        return [m["header"]["msg_type"] for m in self.received if m.get("channel") != "reader"]


def execute(msg_id: str, code: str = "print(1)", **content: Any) -> dict[str, Any]:
    return {
        "channel": "shell",
        "header": {"msg_id": msg_id, "msg_type": "execute_request", "session": "x", "username": "x",
                   "version": "5.3"},
        "parent_header": {},
        "metadata": {},
        "content": {"code": code, "silent": False, "store_history": True, "user_expressions": {},
                    "allow_stdin": True, "stop_on_error": True, **content},
    }


def kernel_msg(channel: str, msg_type: str, parent: str | None, content: dict[str, Any] | None = None,
               **extra: Any) -> dict[str, Any]:
    return {
        "channel": channel,
        "header": {"msg_id": uuid.uuid4().hex, "msg_type": msg_type, "session": "k", "version": "5.3"},
        "parent_header": {"msg_id": parent} if parent else {},
        "metadata": {},
        "content": content or {},
        **extra,
    }


async def settle(times: int = 20) -> None:
    for _ in range(times):
        await asyncio.sleep(0.003)


@dataclass
class Running:
    """A session with an attached FakeBrowser and a running hub."""

    service: Any
    session: Any
    hub: Any
    browser: FakeBrowser
    upstream: FakeWs
    task: asyncio.Task[None]


async def connect(service: Any, up: FakeUpstream, session: Any, recheck: Any = None) -> Running:
    relay = load("reader.kernels.relay")

    async def ok() -> bool:
        return True

    hub = session.hub
    if hub is None or hub.closing:
        hub = relay.Hub(service, session)
        session.hub = hub
        fresh = True
    else:
        fresh = False
    browser = FakeBrowser()
    att = hub.attach(browser, recheck or ok)
    if fresh:
        await hub.start()
    task = asyncio.ensure_future(hub.serve(att))
    await settle(3)
    return Running(service, session, hub, browser, up.sockets.get(session.kernel_id), task)


# Postgres (route tests), as SRV-AUTH's tests set it up


def _daemon_up() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0  # noqa: S607


DOCKER = _daemon_up()
requires_docker = pytest.mark.skipif(not DOCKER, reason="docker daemon not reachable")


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
    name = "k_" + secrets.token_hex(6)
    code, out = container.exec(["psql", "-U", "postgres", "-c", f"CREATE DATABASE {name}"])
    assert code == 0, out
    return Pg(server.host, server.port, server.user, server.password, name)
