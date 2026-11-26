# @trace FR-AGENT-21, INV-AGENT-8, INV-AGENT-10, FR-AGENT-6
"""[db] The judge.find job on the worker-judge lane (ADR-0039 B6, B7b) with a fake CLI, a fake agent-proxy client,
an in-memory request store (SRV-EVENTS's `judge_requests` table is paired), and fake pack guard and inputs sink."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from .conftest import CONFIG_JUDGES, FakeCli, FakeProxyClient, Pg, Script, need, rows, scalar, world

pytestmark = pytest.mark.anyio
D = Decimal
CARDS = (("spn:a", "alpha card"), ("spn:b", "beta card"), ("spn:c", "gamma card"))
GOOD = '{"keep": [2, 1], "drop": [3], "sufficiency": 0.7}'


@dataclass
class MemRequests:
    rows: dict[uuid.UUID, Any] = field(default_factory=dict)
    deleted: list[uuid.UUID] = field(default_factory=list)

    async def get(self, _conn: Any, request_id: uuid.UUID) -> Any:
        return self.rows.get(request_id)

    async def delete(self, _conn: Any, request_id: uuid.UUID) -> None:
        self.deleted.append(request_id)
        self.rows.pop(request_id, None)


@dataclass
class Guard:
    active: bool = True
    calls: list[tuple[str, ...]] = field(default_factory=list)

    async def lock_active(self, _conn: Any, pack_ids: tuple[str, ...]) -> bool:
        self.calls.append(tuple(pack_ids))
        return self.active


@dataclass
class Sink:
    copies: list[uuid.UUID] = field(default_factory=list)

    async def copy(self, _conn: Any, request: Any, _output: Any) -> None:
        self.copies.append(request.request_id)


@dataclass
class Lane:
    w: Any
    deps: Any
    cli: FakeCli
    proxy: FakeProxyClient
    requests: MemRequests
    guard: Guard
    sink: Sink
    calls: list[Any] = field(default_factory=list)


def _lane(w: Any, tmp: Path, scripts: list[Script] | None = None, *, active: bool = True,
          issue_error: str | None = None) -> Lane:
    provider = need("reader.agent.provider")
    lane = need("reader.agent.lane")
    cli = FakeCli(scripts if scripts is not None else [Script(text=GOOD, cost=0.001)])
    proxy = FakeProxyClient(cli, issue_error=issue_error)
    requests, guard, sink = MemRequests(), Guard(active), Sink()
    calls: list[Any] = []

    class Spy:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        async def run(self, call: Any) -> Any:
            calls.append(call)
            return await self.inner.run(call)

    @contextlib.asynccontextmanager
    async def factory(engine: Any) -> AsyncIterator[Any]:
        yield Spy(provider.AgentProvider(engine, w.config, proxy, cli=cli, home_root=tmp))

    deps = lane.Deps(provider=factory, config=lambda: w.config, requests=requests, guard=guard, sink=sink)
    return Lane(w, deps, cli, proxy, requests, guard, sink, calls)


async def _now(w: Any) -> datetime:
    value: datetime = await scalar(w.engine, "SELECT clock_timestamp()")
    return value


async def _request(l: Lane, *, owner: str = "ivan", line: str = "tutor", timeout_s: float = 2.2,
                   age_s: float = 0.0, query: str = "what is a hardened key", cards: tuple[Any, ...] = CARDS,
                   pack_ids: tuple[str, ...] = ("p1",)) -> tuple[Any, str]:
    lane = need("reader.agent.lane")
    judges = need("reader.agent.judges")
    created = await _now(l.w) - timedelta(seconds=age_s)
    owner_id = l.w.users[owner]
    req = lane.JudgeRequest(
        request_id=uuid.uuid4(), owner_id=owner_id, line=line, run_id=uuid.uuid4(), run_cap_usd=D("0.50"),
        lesson_id=None, daily_budget_usd=None, created_at=created,
        deadline=created + timedelta(seconds=timeout_s), query=query, cards=tuple(cards), pack_ids=pack_ids,
    )
    l.requests.rows[req.request_id] = req
    return req, judges.find_input_sha256(owner_id, query, list(cards))


async def _run(l: Lane, req: Any, h: str) -> None:
    lane = need("reader.agent.lane")
    jobs_model = need("reader.jobs.model")
    args = lane.JudgeFindArgs(request_id=req.request_id, input_sha256=h)
    async with l.w.engine.connect() as conn:
        await lane.judge_find(l.deps, args, conn, jobs_model.JobContext(1, str(req.request_id), 1))


async def _outputs(w: Any) -> int:
    n: int = await scalar(w.engine, "SELECT count(*) FROM judge_outputs")
    return n


async def _spend(w: Any) -> list[str]:
    return [r[0] for r in await rows(w.engine, "SELECT kind FROM agent_spend ORDER BY id")]


def test_kind_declaration() -> None:
    lane = need("reader.agent.lane")
    kind = lane.make_kind(lane.Deps(provider=None, config=None, requests=None))  # type: ignore[arg-type]
    assert (kind.name, kind.queue, kind.key, kind.max_attempts) == ("judge.find", "judge", ("request_id",), 1)
    assert set(kind.args.model_fields) == {"request_id", "input_sha256"}
    assert kind.args.model_config.get("extra") == "forbid"


async def test_one_call_records_notifies_and_deletes(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path, CONFIG_JUDGES)
    l = _lane(w, tmp_path)
    req, h = await _request(l)
    heard: list[str] = []
    async with w.engine.connect() as listen:
        raw = await listen.get_raw_connection()
        driver: Any = raw.driver_connection
        await driver.add_listener("judge_done", lambda *a: heard.append(a[3]))
        await listen.commit()
        await _run(l, req, h)
        for _ in range(50):
            if heard:
                break
            await asyncio.sleep(0.02)
    assert heard == [str(req.request_id)]  # the payload is the id only
    assert await _spend(w) == ["reserve", "settle"]
    out = await rows(w.engine, "SELECT owner_id, input_sha256, judge, output, pack_ids, request_id FROM judge_outputs")
    assert len(out) == 1
    owner_id, input_sha, judge, output, packs, request_id = out[0]
    assert (owner_id, input_sha, request_id, list(packs)) == (w.users["ivan"], h, req.request_id, ["p1"])
    assert judge.startswith("judge.find@")
    assert output == {"keep": ["spn:b", "spn:a"], "drop": ["spn:c"], "sufficiency": 0.7}
    assert l.requests.deleted == [req.request_id] and l.sink.copies == [req.request_id]
    assert l.guard.calls == [("p1",)]


async def test_the_stage_call(pg: Pg, tmp_path: Path) -> None:
    judges = need("reader.agent.judges")
    w = await world(pg, tmp_path, CONFIG_JUDGES)
    l = _lane(w, tmp_path)
    req, h = await _request(l, line="compile")
    before = await _now(w)
    await _run(l, req, h)
    (call,) = l.calls
    assert (call.task, call.stage, call.attempt, call.line) == ("judge.find", "judge.find", 1, "compile")
    assert call.handle == "ivan" and call.owner_id == w.users["ivan"]
    assert (call.run_id, call.run_cap_usd, call.daily_budget_usd) == (req.run_id, D("0.50"), None)
    assert call.system == judges.prompt_bytes("judge.find").decode("utf-8")
    assert call.prompt == judges.render_find_input(req.query, list(CARDS))
    assert call.thinking_off and call.abort_on_retry
    assert (call.max_output_tokens, call.floor_usd) == (512, D("0.002"))
    want = (req.created_at + timedelta(seconds=3 * 2.2) - before).total_seconds()
    assert want - 0.5 <= call.deadline_s <= want + 0.01


async def test_handle_comes_from_users(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path, CONFIG_JUDGES)
    l = _lane(w, tmp_path)
    req, h = await _request(l, owner="anna")
    await _run(l, req, h)
    assert [c.handle for c in l.calls] == ["anna"]
    assert l.proxy.issued[0][0] == "anna"


@pytest.mark.parametrize("case", ["no-row", "eval-line", "deadline-passed", "hash-mismatch", "no-time-left"])
async def test_no_call(pg: Pg, tmp_path: Path, case: str) -> None:
    w = await world(pg, tmp_path, CONFIG_JUDGES)
    l = _lane(w, tmp_path)
    if case == "eval-line":
        req, h = await _request(l, line="eval")
    elif case == "deadline-passed":
        req, h = await _request(l, age_s=3.0)
    elif case == "no-time-left":  # past the deadline and the kill window
        req, h = await _request(l, timeout_s=1.0, age_s=4.0)
    else:
        req, h = await _request(l)
    if case == "no-row":
        l.requests.rows.clear()
    if case == "hash-mismatch":
        h = "0" * 64
    await _run(l, req, h)
    assert l.calls == [] and l.cli.requests == []
    assert await _spend(w) == [] and await _outputs(w) == 0
    if case != "no-row":
        assert l.requests.deleted == [req.request_id]


@pytest.mark.parametrize(("case", "code"), [("window", "judge_window_too_long"), ("big", "judge_input_too_large")])
async def test_refused_requests_raise_without_a_call(pg: Pg, tmp_path: Path, case: str, code: str) -> None:
    errors = need("reader.agent.errors")
    w = await world(pg, tmp_path, CONFIG_JUDGES)
    l = _lane(w, tmp_path)
    if case == "window":  # 3 × 9 s = 27 s > 30 − 5
        req, h = await _request(l, timeout_s=9.0)
    else:  # 6,200 characters of card text is over 3,000 estimated tokens at 2.0 chars per token
        req, h = await _request(l, cards=(("spn:a", "x" * 6200),))
    with pytest.raises(errors.AgentError) as refused:
        await _run(l, req, h)
    assert refused.value.code == code
    assert l.calls == [] and await _spend(w) == []
    assert l.requests.deleted == [req.request_id]


async def test_recorded_hit_makes_no_call(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path, CONFIG_JUDGES)
    l = _lane(w, tmp_path, [Script(text=GOOD, cost=0.001)])
    req, h = await _request(l)
    await _run(l, req, h)
    req2, h2 = await _request(l)
    assert h2 == h
    await _run(l, req2, h2)
    assert len(l.calls) == 1 and await _outputs(w) == 1
    assert l.requests.deleted == [req.request_id, req2.request_id]


async def test_concurrent_identical_jobs_write_once(pg: Pg, tmp_path: Path) -> None:
    """INV-AGENT-8: four concurrent jobs, one process, HOME and token each; one output row, one sink copy."""
    w = await world(pg, tmp_path, CONFIG_JUDGES)
    l = _lane(w, tmp_path, [Script(text=GOOD, cost=0.001, sleep_s=0.05) for _ in range(4)])
    reqs = [await _request(l) for _ in range(4)]
    await asyncio.gather(*(_run(l, r, h) for r, h in reqs))
    assert len(l.calls) == 4 and len(l.cli.requests) == 4
    homes = {r.home for r in l.cli.requests}
    tokens = {r.env["CLAUDE_CODE_OAUTH_TOKEN"] for r in l.cli.requests}
    assert len(homes) == 4 and len(tokens) == 4 and len(l.proxy.issued) == 4
    assert sorted(l.proxy.revoked) == sorted(tokens)
    assert await _outputs(w) == 1 and len(l.sink.copies) == 1
    reserves = await scalar(w.engine, "SELECT count(*) FROM agent_spend WHERE kind = 'reserve'")
    assert reserves == 4


async def test_late_output_is_recorded(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path, CONFIG_JUDGES)
    l = _lane(w, tmp_path, [Script(text=GOOD, cost=0.001, sleep_s=0.4)])
    req, h = await _request(l, timeout_s=0.3)  # the waiter gives up at 0.3 s; the kill window is 0.9 s
    await _run(l, req, h)
    assert await _outputs(w) == 1


async def test_guard_false_writes_nothing(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path, CONFIG_JUDGES)
    l = _lane(w, tmp_path, active=False)
    req, h = await _request(l)
    await _run(l, req, h)
    assert await _outputs(w) == 0 and l.sink.copies == []
    assert l.requests.deleted == [req.request_id]


@pytest.mark.parametrize("case", ["output-invalid", "budget", "lane-saturated"])
async def test_failures_leave_no_output_and_delete_the_request(pg: Pg, tmp_path: Path, case: str) -> None:
    w = await world(pg, tmp_path, CONFIG_JUDGES)
    if case == "output-invalid":
        l = _lane(w, tmp_path, [Script(text="not json", cost=0.001)])
    elif case == "lane-saturated":
        l = _lane(w, tmp_path, issue_error="lane_saturated")
    else:
        l = _lane(w, tmp_path)
    req, h = await _request(l)
    if case == "budget":
        req = replace(req, run_cap_usd=D("0.001"))
        l.requests.rows[req.request_id] = req
    await _run(l, req, h)  # no exception: every AgentError ends the job quietly
    assert await _outputs(w) == 0
    assert l.requests.deleted == [req.request_id]
    if case == "lane-saturated":
        settled = await rows(w.engine, "SELECT settled_usd, upstream FROM agent_spend WHERE kind = 'settle'")
        assert [(s[0], s[1]["statuses"]) for s in settled] == [(D("0"), [])]


async def test_unexpected_error_still_deletes_the_request(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path, CONFIG_JUDGES)
    l = _lane(w, tmp_path)

    class Boom(Sink):
        async def copy(self, _conn: Any, request: Any, _output: Any) -> None:
            raise RuntimeError("sink down")

    l.deps = replace(l.deps, sink=Boom())
    req, h = await _request(l)
    with pytest.raises(RuntimeError):
        await _run(l, req, h)
    assert l.requests.deleted == [req.request_id]


async def test_no_text_outside_the_request(pg: Pg, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """INV-AGENT-10: marker strings in the query and every card appear in no args, ledger row, output or log."""
    caplog.set_level(logging.DEBUG)
    lane = need("reader.agent.lane")
    w = await world(pg, tmp_path, CONFIG_JUDGES)
    l = _lane(w, tmp_path)
    cards = (("spn:a", "CANARY-CARD-ONE text"), ("spn:b", "CANARY-CARD-TWO text"), ("spn:c", "CANARY-CARD-3"))
    req, h = await _request(l, query="CANARY-QUERY words", cards=cards)
    await _run(l, req, h)
    args = lane.JudgeFindArgs(request_id=req.request_id, input_sha256=h).model_dump_json()
    dumps = [args, caplog.text]
    dumps += [json.dumps([str(c) for c in r]) for r in await rows(w.engine, "SELECT * FROM agent_spend")]
    dumps += [json.dumps([str(c) for c in r]) for r in await rows(w.engine, "SELECT * FROM judge_outputs")]
    assert await _outputs(w) == 1
    for dump in dumps:
        assert "CANARY" not in dump


async def test_start_hook_revokes_all_and_never_raises() -> None:
    lane = need("reader.agent.lane")

    class Proxy:
        def __init__(self, fail: bool) -> None:
            self.fail, self.calls, self.closed = fail, 0, False

        async def revoke_all(self) -> int | None:
            self.calls += 1
            if self.fail:
                raise RuntimeError("proxy down")
            return 3

        async def aclose(self) -> None:
            self.closed = True

    for fail in (False, True):
        proxy = Proxy(fail)
        await lane.on_worker_start(None, proxy=proxy)
        assert proxy.calls == 1 and proxy.closed


def test_lane_proxy_client_timeout() -> None:
    lane = need("reader.agent.lane")
    client = lane.proxy_client()
    assert client.client.timeout.connect == 1.0 and client.client.timeout.read == 1.0
