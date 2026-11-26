# @trace FR-AGENT-3, INV-AGENT-7, VEC-AGENT-6
"""agent-proxy's client identity (ADR-0039 B7b): the client is named by the connection's source address on agentnet,
each client has its own quota and TTL, the lane is refused rather than evicting, revoke-all, no cross-client revoke
or forward, unknown addresses get nothing, and main()'s bind. In process (ASGI) against the recording stub."""

from __future__ import annotations

import asyncio
import copy
import logging
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from .conftest import (
    FIXTURES,
    LANE_ADDR,
    WORKER_ADDR,
    Upstream,
    forward,
    issue,
    load_yaml,
    make_proxy,
    message_body,
    need,
    spec_bounds,
)

pytestmark = pytest.mark.anyio
UNKNOWN_ADDR = "10.230.7.200"
ADDRESSES = {"worker": WORKER_ADDR, "lane": LANE_ADDR, "unknown": UNKNOWN_ADDR}
NAMES = {"worker": "worker", "lane": "worker-judge"}
SEQUENCES = load_yaml(FIXTURES / "client-sequences.yaml")["sequences"]
ALLOW = {"searches_max": 1, "searches_per_request_max": 1, "requests_max": 3}
WEB_TOOLS = [{"type": "web_search_20250305", "name": "web_search"}]


def _issue_body(**extra: Any) -> dict[str, Any]:
    return {"user": "ivan", "max_output_tokens": 16000, "model": "claude-sonnet-5-5", **extra}


def test_clients_table_matches_the_bounds_and_the_deploy_addresses() -> None:
    proxy = need("reader.agent.proxy")
    b = spec_bounds()
    by_name = {c.name: (address, c) for address, c in proxy.CLIENTS.items()}
    assert set(by_name) == {"worker", "worker-judge"}
    worker_addr, worker = by_name["worker"]
    lane_addr, lane = by_name["worker-judge"]
    assert (worker_addr, lane_addr) == (WORKER_ADDR, LANE_ADDR)
    assert (worker.quota, worker.ttl_s, worker.tools, worker.evict) == (
        b["client_quota"]["worker"], b["run_token_ttl_s"], True, True
    )
    assert (lane.quota, lane.ttl_s, lane.tools, lane.evict) == (
        b["client_quota"]["worker-judge"], b["lane_token_ttl_s"], False, False
    )
    assert worker.quota + lane.quota == b["run_tokens_max"]
    assert proxy.ProxySettings().clients == proxy.CLIENTS


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/run-tokens", _issue_body()),
        ("POST", "/run-tokens/revoke", {"run_token": "x" * 43}),
        ("POST", "/run-tokens/revoke-all", {}),
        ("POST", "/v1/messages?beta=true", None),
        ("GET", "/anything", None),
    ],
)
async def test_unknown_address_is_403_on_every_route(
    tmp_path: Path, method: str, path: str, body: dict[str, Any] | None
) -> None:
    p = make_proxy(tmp_path)
    async with p.client(UNKNOWN_ADDR) as c:
        r = await c.request(method, path, json=body) if body is not None else await c.request(
            method, path, content=message_body()
        )
    assert (r.status_code, r.json()) == (403, {"error": "client_unknown"})
    assert p.upstream.requests == []


async def test_forwarded_for_header_is_ignored(tmp_path: Path) -> None:
    p = make_proxy(tmp_path)
    async with p.client(UNKNOWN_ADDR) as c:
        r = await c.post("/run-tokens", json=_issue_body(), headers={"x-forwarded-for": WORKER_ADDR})
    assert r.status_code == 403


async def test_lane_quota_never_evicts_and_purge_frees_a_slot(tmp_path: Path) -> None:
    p = make_proxy(tmp_path)
    b = spec_bounds()
    async with p.client() as worker, p.client(LANE_ADDR) as lane:
        w = await issue(worker)
        tokens = [await issue(lane) for _ in range(b["client_quota"]["worker-judge"])]
        r = await lane.post("/run-tokens", json=_issue_body())
        assert (r.status_code, r.json()) == (429, {"error": "client_quota"})
        assert (await forward(worker, w)).status_code == 200  # never evicted by the lane
        assert [(await forward(lane, t)).status_code for t in tokens] == [200] * len(tokens)
        p.clock[0] += b["lane_token_ttl_s"] + 1
        fresh = await issue(lane)  # the expired lane tokens were purged
        assert (await lane.post("/run-tokens/revoke", json={"run_token": tokens[0]})).status_code == 404
        assert (await forward(lane, fresh)).status_code == 200
        assert (await forward(worker, w)).status_code == 200  # the worker's TTL is its own


async def test_worker_evicts_only_its_own_oldest(tmp_path: Path) -> None:
    p = make_proxy(tmp_path)
    b = spec_bounds()
    async with p.client() as worker, p.client(LANE_ADDR) as lane:
        lane_token = await issue(lane)
        first = await issue(worker)
        for _ in range(b["client_quota"]["worker"]):
            await issue(worker)
        assert (await forward(worker, first)).status_code == 401
        assert (await forward(lane, lane_token)).status_code == 200


async def test_no_cross_client_revoke_or_forward(tmp_path: Path) -> None:
    p = make_proxy(tmp_path)
    async with p.client() as worker, p.client(LANE_ADDR) as lane:
        w, ln = await issue(worker), await issue(lane)
        r = await lane.post("/run-tokens/revoke", json={"run_token": w})
        assert (r.status_code, r.json()) == (404, {"error": "run_token_unknown"})
        assert (await forward(worker, w)).status_code == 200
        assert (await forward(worker, ln)).status_code == 401  # a lane token used from the worker address
        assert (await forward(lane, w)).status_code == 401


async def test_revoke_all_revokes_the_callers_tokens_only(tmp_path: Path) -> None:
    """The killed-lane case: 4 lane tokens never revoked, then the restarted lane's revoke-all."""
    p = make_proxy(tmp_path)
    async with p.client() as worker, p.client(LANE_ADDR) as lane:
        w = await issue(worker)
        for _ in range(4):
            await issue(lane)
        r = await lane.post("/run-tokens/revoke-all", json={})
        assert (r.status_code, r.json()) == (200, {"revoked": 4})
        assert p.app.state.proxy.live("worker-judge") == 0
        assert p.app.state.proxy.live("worker") == 1
        assert (await forward(worker, w)).status_code == 200
        assert (await worker.post("/run-tokens/revoke-all", json={})).json() == {"revoked": 1}


async def test_lane_is_never_given_a_tool_allowance(tmp_path: Path) -> None:
    p = make_proxy(tmp_path)
    async with p.client(LANE_ADDR) as lane:
        for task in ("freshness_web", "judge.find", None):
            extra: dict[str, Any] = {"web_search": ALLOW} | ({"task": task} if task else {})
            r = await lane.post("/run-tokens", json=_issue_body(**extra))
            assert (r.status_code, r.json()) == (400, {"error": "tools_not_allowed"}), task
        assert p.app.state.proxy.live("worker-judge") == 0


async def test_concurrent_lane_issues_leave_exactly_the_quota(tmp_path: Path) -> None:
    p = make_proxy(tmp_path)
    async with p.client(LANE_ADDR) as lane:
        results = await asyncio.gather(*(lane.post("/run-tokens", json=_issue_body()) for _ in range(8)))
    assert sorted(r.status_code for r in results) == [200] * 4 + [429] * 4
    assert p.app.state.proxy.live("worker-judge") == 4


async def test_revoke_all_logs_its_template_only(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    p = make_proxy(tmp_path)
    caplog.set_level(logging.INFO, logger="reader.agent.proxy")
    async with p.client(LANE_ADDR) as lane:
        token = await issue(lane)
        await lane.post("/run-tokens/revoke-all", json={})
    lines = [r.getMessage() for r in caplog.records if "/run-tokens/revoke-all" in r.getMessage()]
    assert len(lines) == 1
    assert lines[0].startswith("POST /run-tokens/revoke-all 200 ")
    assert token not in caplog.text and LANE_ADDR not in caplog.text


@pytest.mark.parametrize(
    ("argv", "host"), [([], "0.0.0.0"), (["--bind", "10.230.7.2:8080"], "10.230.7.2")]  # noqa: S104
)
def test_main_binds_the_given_address_without_proxy_headers(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], host: str
) -> None:
    import uvicorn

    proxy = need("reader.agent.proxy")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(kw))
    proxy.main(argv)
    assert calls[0]["host"] == host
    assert calls[0]["port"] == 8080
    assert calls[0]["proxy_headers"] is False


@pytest.mark.parametrize("bind", ["10.230.7.2", "10.230.7.2:http", "nowhere:8080", ":8080"])
def test_main_refuses_a_malformed_bind(monkeypatch: pytest.MonkeyPatch, bind: str) -> None:
    import uvicorn

    proxy = need("reader.agent.proxy")
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)
    with pytest.raises(SystemExit) as caught:
        proxy.main(["--bind", bind])
    assert caught.value.code == 2


@pytest.mark.parametrize("seq", SEQUENCES, ids=lambda s: s["id"])
async def test_client_sequences(seq: dict[str, Any], tmp_path: Path) -> None:
    """VEC-AGENT-6."""
    p = make_proxy(tmp_path)
    tokens: dict[str, str] = {}
    clients = {k: p.client(a) for k, a in ADDRESSES.items()}
    try:
        for step in seq["steps"]:
            c = clients[step["from"]]
            op = step["op"]
            if op == "advance":
                p.clock[0] += step["seconds"]
                continue
            if op == "issue_many":
                for _ in range(step["count"]):
                    assert (await c.post("/run-tokens", json=_issue_body())).status_code == step["expect"]
                r = None
            elif op in ("issue", "issue_web"):
                extra = {"task": "freshness_web", "web_search": ALLOW} if op == "issue_web" else {}
                r = await c.post("/run-tokens", json=_issue_body(**extra))
                if r.status_code == 200:
                    tokens[step["as"]] = r.json()["run_token"]
            elif op == "revoke":
                r = await c.post("/run-tokens/revoke", json={"run_token": tokens.get(step["ref"], "x" * 43)})
            elif op == "revoke_all":
                r = await c.post("/run-tokens/revoke-all", json={})
            else:
                r = await forward(c, tokens.get(step["ref"], "x" * 43))
            if r is not None:
                assert r.status_code == step["expect"], (seq["id"], step, r.text)
                if "error" in step:
                    assert r.json() == {"error": step["error"]}, step
                if "revoked" in step:
                    assert r.json() == {"revoked": step["revoked"]}, step
            if "live" in step:
                live = {k: p.app.state.proxy.live(NAMES[k]) for k in ("worker", "lane")}
                assert live == step["live"], (seq["id"], step)
    finally:
        for c in clients.values():
            await c.aclose()


class ClientMachine(RuleBasedStateMachine):
    """INV-AGENT-7: random issue, revoke, revoke-all, forward and clock steps from both clients and an unknown
    address, against a stub upstream that records which user token each forwarded request carried."""

    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.tokens: dict[str, list[str]] = {"worker": [], "lane": [], "unknown": []}

    @initialize()
    def start(self) -> None:
        import tempfile

        self.tmp = Path(tempfile.mkdtemp())
        self.upstream = Upstream()
        self.p = make_proxy(self.tmp, self.upstream)
        self.clients = {k: self.p.client(a) for k, a in ADDRESSES.items()}

    def _run(self, coro: Any) -> Any:
        return self.loop.run_until_complete(coro)

    @rule(who=st.sampled_from(["worker", "lane", "unknown"]), web=st.booleans())
    def issue(self, who: str, web: bool) -> None:
        extra = {"task": "freshness_web", "web_search": ALLOW} if web else {}
        r = self._run(self.clients[who].post("/run-tokens", json=_issue_body(**extra)))
        if who == "unknown":
            assert r.status_code == 403
        elif who == "lane" and web:
            assert r.status_code == 400
        if r.status_code == 200:
            self.tokens[who].append(r.json()["run_token"])

    @rule(who=st.sampled_from(["worker", "lane", "unknown"]), owner=st.sampled_from(["worker", "lane"]),
          i=st.integers(0, 40))
    def revoke(self, who: str, owner: str, i: int) -> None:
        pool = self.tokens[owner]
        if not pool:
            return
        token = pool[i % len(pool)]
        before = {k: self.p.app.state.proxy.live(NAMES[k]) for k in ("worker", "lane")}
        r = self._run(self.clients[who].post("/run-tokens/revoke", json={"run_token": token}))
        if who != owner:
            assert r.status_code in (403, 404)
            assert {k: self.p.app.state.proxy.live(NAMES[k]) for k in ("worker", "lane")} == before

    @rule(who=st.sampled_from(["worker", "lane"]))
    def revoke_all(self, who: str) -> None:
        other = "lane" if who == "worker" else "worker"
        before = self.p.app.state.proxy.live(NAMES[other])
        r = self._run(self.clients[who].post("/run-tokens/revoke-all", json={}))
        assert r.status_code == 200
        assert self.p.app.state.proxy.live(NAMES[who]) == 0
        assert self.p.app.state.proxy.live(NAMES[other]) == before

    @rule(who=st.sampled_from(["worker", "lane", "unknown"]), owner=st.sampled_from(["worker", "lane"]),
          i=st.integers(0, 40), tools=st.booleans())
    def forward(self, who: str, owner: str, i: int, tools: bool) -> None:
        pool = self.tokens[owner]
        if not pool:
            return
        sent_before = len(self.upstream.requests)
        body = message_body(tools=copy.deepcopy(WEB_TOOLS)) if tools else None
        r = self._run(forward(self.clients[who], pool[i % len(pool)], body=body))
        if who != owner:
            assert r.status_code in (401, 403)
            assert len(self.upstream.requests) == sent_before

    @rule(seconds=st.sampled_from([1, 10, 31, 1501]))
    def advance(self, seconds: int) -> None:
        self.p.clock[0] += seconds

    @invariant()
    def quotas_hold(self) -> None:
        if not hasattr(self, "p"):
            return
        b = spec_bounds()["client_quota"]
        assert self.p.app.state.proxy.live("worker") <= b["worker"]
        assert self.p.app.state.proxy.live("worker-judge") <= b["worker-judge"]
        assert not any(e.allowance for e in self.p.app.state.proxy.tokens.values() if e.client == "worker-judge")

    def teardown(self) -> None:
        if hasattr(self, "clients"):
            for c in self.clients.values():
                self._run(c.aclose())
        self.loop.close()


ClientMachine.TestCase.settings = settings(
    max_examples=40, stateful_step_count=30, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
TestClientMachine = ClientMachine.TestCase
