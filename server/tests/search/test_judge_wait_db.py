"""SRV-SEARCH [db]: the `judge.find` waiter (FR-SEARCH-28) with a fake lane that reads the row, records an
output and notifies; its invariants (INV-SEARCH-11, -14), deterministic responses (INV-SEARCH-4, revised) and
the judge-request parts of INV-SEARCH-12 and INV-SEARCH-1 (revised). No model is called anywhere.

@trace FR-SEARCH-28 INV-SEARCH-11 INV-SEARCH-14 INV-SEARCH-4 INV-SEARCH-12 INV-SEARCH-1
"""

from __future__ import annotations

import asyncio
import copy
import json
import random
import time
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from .conftest import MAT_EN, EmbedServer, Pg, World, add_material, embed_material, need, world

pytestmark = pytest.mark.anyio

CONFIG = """\
version: 1
instance: { url: "https://localhost", data_dir: ./data }
users:
  - handle: ivan
    role: owner
    providers:
      - { kind: anthropic-subscription, token: "secret://keychain/reader-ai/claude-oauth-token" }
  - { handle: anna, role: learner }
materials:
  - { id: book-ru, file: ./ru.pdf, sha256: "%s", license: test, language: ru }
  - { id: book-en, file: ./en.pdf, sha256: "%s", license: test, language: en }
tasks:
  judge.find: { model: haiku }
sharing: {}
"""
BRAIN = "b" * 64
PACK = "c" * 64


@pytest.fixture
async def w(pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    built = await world(pg, tmp_path, monkeypatch)
    built.config_path.write_text(CONFIG % ("a" * 64, MAT_EN), encoding="utf-8")
    try:
        yield built
    finally:
        await need("reader.search.judge_wait").stop(built.engine)
        await built.engine.dispose()


def _policy(timeout_s: float = 2.0, mode: str = "always_judge") -> Any:
    policy = need("reader.search.policy")
    loaded = policy.load()
    data = copy.deepcopy(dict(loaded.data))
    data["gate"] = {"mode": mode, "margin": None}
    data["judge"]["timeout_s"] = timeout_s
    return policy.Policy(data, loaded.facets_sha256)


def _card(card_id: str, score: float, body: str, license_: str = "personal-use") -> Any:
    cards = need("reader.search.cards")
    return cards.Card(
        id=card_id, layer="L1", title="", text=body, cite=(cards.Cite(spn=card_id, quote_sha256="q" * 64),),
        source=cards.Source(id="s" * 64, role=None, as_of=None, reliability=None, license=license_),
        score=score, covers=frozenset({f"l:{card_id}"}), pack_id=PACK,
    )


def cards3() -> list[Any]:
    return [_card(f"c{i}", 0.9 - i / 10, f"card text number {i}") for i in range(3)]


@dataclass
class Corpus:
    cards: list[Any]
    calls: int = 0

    async def candidates(self, caller: Any, channel: str, route: Any, query: str, filters: Any,
                         identifiers: Sequence[str]) -> Any:
        self.calls += 1
        find = need("reader.search.find")
        return find.Candidates(cards=list(self.cards), degraded=False, brain_version=BRAIN,
                               lexemes=frozenset(), layers=frozenset({"L1"}))

    async def neighbours(self, caller: Any, channel: str, cards: Sequence[Any]) -> Mapping[str, list[Any]]:
        return {}


def judge_name() -> str:
    judges = need("reader.agent.judges")
    from reader.agent.pricing import resolve_model

    name: str = judges.find_judge(resolve_model("haiku"))
    return name


@dataclass
class Lane:
    """The lane's side: enqueue → read the row → record an output (keep = the first `keep` cards) → delete the
    row and notify `<request_id>:<code>` (or the bare id) in one transaction, before enqueue returns."""

    engine: Any
    code: str | None = "ok"  # None: never notifies
    record: bool = True
    keep: int = 2
    bare: bool = False
    fail: bool = False
    jobs: Any = None
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, kind: str, args: Mapping[str, Any]) -> Any:
        self.calls.append((kind, dict(args)))
        if self.fail:
            raise RuntimeError("enqueue failed")
        result = await self.jobs.enqueue(kind, args) if self.jobs is not None else None
        rid = uuid.UUID(str(args["request_id"]))
        from reader.jobs.judge_requests import STORE

        async with self.engine.begin() as conn:
            got = await STORE.get(conn, rid)
            extra = (await conn.execute(text(
                "SELECT input_sha256, created_at, deadline, now() AS seen FROM judge_requests"
                " WHERE request_id = :r"), {"r": rid})).mappings().first()
            assert got is not None and extra is not None
            self.rows.append({"request": got, **dict(extra)})
            if self.code is None:
                return result
            if self.record:
                ids = [c for c, _ in got.cards]
                output = {"keep": ids[: self.keep], "drop": ids[self.keep:], "sufficiency": 0.5}
                judges = need("reader.agent.judges")
                await judges.record(conn, got.owner_id, extra["input_sha256"], judge_name(), output,
                                    pack_ids=got.pack_ids, request_id=rid)
            await STORE.delete(conn, rid)
            payload = str(rid) if self.bare else f"{rid}:{self.code}"
            await conn.execute(text("SELECT pg_notify('judge_done', :p)"), {"p": payload})
        return result


@dataclass
class Queued:
    kind: str = "queued"


def waiter(w: World, lane: Lane, policy: Any = None, **kw: Any) -> Any:
    async def enqueue(kind: str, args: Mapping[str, Any]) -> Any:
        found = await lane(kind, args)
        return found if found is not None else Queued()

    judge_wait = need("reader.search.judge_wait")
    return judge_wait.Waiter(engine=w.engine, enqueue=enqueue, policy=policy or _policy(), **kw)


async def run(w: World, judge: Any, cards: list[Any], *, channel: str = "owner", ctx: Any = None,
              provider: bool = True, user: str = "ivan", query: str = "a query", corpus: Any = None,
              policy: Any = None, budget: int | None = None) -> dict[str, Any]:
    find = need("reader.search.find")
    chosen = policy or (judge.policy if judge is not None else _policy())
    deps = find.Deps(corpus=corpus or Corpus(cards), judge=judge, policy=chosen,
                     policy_version=lambda: "p" * 64)
    caller = find.Caller(w.users[user], ("owner",), provider)
    result: dict[str, Any] = await find.find(caller, channel, query, deps=deps, judge_ctx=ctx, budget=budget)
    return result


async def count(w: World, sql: str, **params: Any) -> int:
    async with w.engine.connect() as conn:
        return int((await conn.execute(text(sql), params)).scalar() or 0)


def tutor_ctx() -> dict[str, Any]:
    return {"line": "tutor", "run_id": uuid.uuid4(), "run_cap_usd": Decimal("0.05"),
            "lesson_id": uuid.uuid4(), "daily_budget_usd": Decimal("1.5")}


# ---- FR-SEARCH-28: the waiter ------------------------------------------------------------------------------


async def test_a_live_output_in_time_is_applied(w: World) -> None:
    lane = Lane(w.engine)
    out = await run(w, waiter(w, lane), cards3())
    assert (out["judge_ran"], out["judge_source"], out["degraded"]) == (True, "live", False)
    assert [(c["id"], c["judged"]) for c in out["cards"]][:2] == [("c0", True), ("c1", True)]
    assert len(lane.calls) == 1  # exactly one row and one job per find
    kind, args = lane.calls[0]
    assert kind == "judge.find" and set(args) == {"request_id", "input_sha256"}
    row = lane.rows[0]
    assert str(row["request"].request_id) == str(args["request_id"])
    assert row["input_sha256"] == args["input_sha256"]
    judges = need("reader.agent.judges")
    assert args["input_sha256"] == judges.find_input_sha256(w.users["ivan"], "a query", row["request"].cards)
    assert await count(w, "SELECT count(*) FROM judge_requests") == 0


@pytest.mark.parametrize(
    ("code", "record", "bare", "expected"),
    [
        ("ok", True, False, ("live", None)),
        ("recorded", True, False, ("live", None)),
        ("ok", False, False, (None, "error")),  # ok without a recorded output
        ("budget", False, False, (None, "budget")),
        ("saturated", False, False, (None, "saturated")),
        ("quota_refused", False, False, (None, "quota_refused")),
        ("output_invalid", False, False, (None, "error")),
        ("expired", False, False, (None, "error")),
        ("error", False, False, (None, "error")),
        ("bogus", True, False, (None, "error")),
        ("ok", True, True, ("live", None)),  # interim rule: a bare id reads as ok
    ],
)
async def test_each_payload_code_maps(w: World, code: str, record: bool, bare: bool,
                                      expected: tuple[str | None, str | None]) -> None:
    out = await run(w, waiter(w, Lane(w.engine, code=code, record=record, bare=bare)), cards3())
    source, skip = expected
    assert out.get("judge_source") == source and out.get("judge_skip") == skip
    assert out["degraded"] is (skip is not None)


def test_parse_reads_the_closed_set_and_a_bare_id() -> None:
    parse = need("reader.search.judge_wait").parse
    rid = str(uuid.uuid4())
    assert parse(f"{rid}:budget") == (rid, "budget")
    assert parse(rid) == (rid, "ok")
    assert parse(f"{rid}:nonsense") == (rid, "error")


async def test_no_notification_in_time_is_a_timeout(w: World) -> None:
    lane = Lane(w.engine, code=None)
    start = time.monotonic()
    out = await run(w, waiter(w, lane, _policy(timeout_s=0.4)), cards3())
    assert time.monotonic() - start <= 0.4 + 0.5
    assert (out["judge_skip"], out["degraded"], out["judge_ran"]) == ("timeout", True, False)
    assert out["cards"] and all(not c["judged"] for c in out["cards"])


async def test_a_recorded_hit_makes_no_row_and_no_job(w: World) -> None:
    lane = Lane(w.engine)
    first = await run(w, waiter(w, lane), cards3())
    assert first["judge_source"] == "live" and len(lane.calls) == 1
    again = await run(w, waiter(w, lane), cards3())
    assert (again["judge_source"], again["judge_ran"]) == ("recorded", True)
    assert len(lane.calls) == 1  # no second row, no second job
    assert again["cards"] == first["cards"]  # INV-SEARCH-4: a replayed output gives the same cards
    third = await run(w, waiter(w, lane), cards3())
    assert json.dumps(again, sort_keys=True) == json.dumps(third, sort_keys=True)


async def test_degrades_without_a_row_or_job(w: World) -> None:
    lane = Lane(w.engine)
    assert (await run(w, waiter(w, lane), cards3(), provider=False))["judge_skip"] == "no_provider"
    assert (await run(w, waiter(w, lane), cards3(), channel="tutor"))["judge_skip"] == "error"  # no ctx
    w.config_path.write_text((CONFIG % ("a" * 64, MAT_EN)).replace("  judge.find: { model: haiku }\n", ""),
                             encoding="utf-8")
    assert (await run(w, waiter(w, lane), cards3()))["judge_skip"] == "error"  # the slot is missing
    assert lane.calls == []
    assert await count(w, "SELECT count(*) FROM judge_requests") == 0


async def test_the_owner_channel_builds_its_context(w: World) -> None:
    lane = Lane(w.engine)
    policy = _policy()
    for i in range(2):
        await run(w, waiter(w, lane, policy, daily_budget_usd=Decimal("3")), cards3(), query=f"q {i}")
    first, second = (r["request"] for r in lane.rows)
    assert first.line == second.line == "mcp"
    assert first.run_id != second.run_id  # a fresh run per request
    assert first.run_cap_usd == Decimal(str(policy.data["owner_judge_run_cap_usd"]))
    assert first.lesson_id is None and first.daily_budget_usd == Decimal("3")


async def test_a_caller_given_context_is_used_unchanged(w: World) -> None:
    lane = Lane(w.engine)
    ctx = tutor_ctx()
    out = await run(w, waiter(w, lane), cards3(), channel="tutor", ctx=ctx)
    assert out["judge_source"] == "live"
    got = lane.rows[0]["request"]
    assert (got.line, got.run_id, got.run_cap_usd, got.lesson_id, got.daily_budget_usd) == (
        ctx["line"], ctx["run_id"], ctx["run_cap_usd"], ctx["lesson_id"], ctx["daily_budget_usd"])


async def test_created_at_and_deadline_come_from_the_database_clock(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    import datetime as dt

    judge_wait = need("reader.search.judge_wait")

    class Skewed(dt.datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return dt.datetime(2001, 1, 1, tzinfo=tz or dt.UTC)

    for name in ("datetime",):  # a process clock 25 years off, wherever the waiter could read it
        if hasattr(judge_wait, name):
            monkeypatch.setattr(judge_wait, name, Skewed)
    lane = Lane(w.engine)
    await run(w, waiter(w, lane, _policy(timeout_s=1.5)), cards3())
    row = lane.rows[0]
    assert row["deadline"] - row["created_at"] == timedelta(seconds=1.5)
    assert abs((row["seen"] - row["created_at"]).total_seconds()) < 30  # the database's now, not 2001


async def test_an_enqueue_failure_deletes_the_row(w: World) -> None:
    out = await run(w, waiter(w, Lane(w.engine, fail=True)), cards3())
    assert (out["judge_skip"], out["degraded"]) == ("error", True)
    assert await count(w, "SELECT count(*) FROM judge_requests") == 0


async def test_a_listener_that_is_down_degrades_with_no_row(w: World) -> None:
    judge_wait = need("reader.search.judge_wait")
    broken = judge_wait.Listener(w.engine)

    async def down() -> None:
        raise OSError("no connection")

    broken.start = down
    lane = Lane(w.engine)
    out = await run(w, waiter(w, lane, listener=broken), cards3())
    assert out["judge_skip"] == "error" and lane.calls == []
    assert await count(w, "SELECT count(*) FROM judge_requests") == 0


async def test_the_listener_restarts_after_a_lost_connection(w: World) -> None:
    lane = Lane(w.engine)
    assert (await run(w, waiter(w, lane), cards3()))["judge_source"] == "live"
    async with w.engine.begin() as conn:
        await conn.execute(text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
            " WHERE datname = current_database() AND query ILIKE 'LISTEN%' AND pid <> pg_backend_pid()"))
    await asyncio.sleep(0.3)
    assert (await run(w, waiter(w, lane), cards3(), query="another"))["judge_source"] == "live"


KILL_LISTEN = ("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = current_database()"
               " AND query ILIKE 'LISTEN%' AND pid <> pg_backend_pid()")


async def test_a_connection_lost_mid_wait_resolves_the_waiters_with_error(w: World) -> None:
    found = need("reader.search.judge_wait").listener(w.engine)
    silent = Lane(w.engine, code=None)
    judge = waiter(w, silent, _policy(timeout_s=6.0))
    tasks = [asyncio.create_task(run(w, judge, cards3(), query=f"q {i}")) for i in range(3)]
    for _ in range(100):  # until all three are registered and waiting
        if found.pending() == 3 and len(silent.calls) == 3:
            break
        await asyncio.sleep(0.05)
    assert found.pending() == 3
    start = time.monotonic()
    async with w.engine.begin() as conn:
        await conn.execute(text(KILL_LISTEN))
    outs = await asyncio.gather(*tasks)
    assert time.monotonic() - start < 3.0  # resolved by the loss, not at the 6 s timeout
    assert all((o["judge_skip"], o["degraded"]) == ("error", True) for o in outs)
    assert found.pending() == 0
    assert (await run(w, waiter(w, Lane(w.engine)), cards3(), query="next"))["judge_source"] == "live"
    assert found.pending() == 0


async def test_every_exit_leaves_no_registered_waiter(w: World) -> None:
    found = need("reader.search.judge_wait").listener(w.engine)
    assert (await run(w, waiter(w, Lane(w.engine)), cards3()))["judge_source"] == "live"
    assert found.pending() == 0  # applied
    out = await run(w, waiter(w, Lane(w.engine, code=None), _policy(timeout_s=0.3)), cards3(), query="t")
    assert out["judge_skip"] == "timeout" and found.pending() == 0  # timed out
    silent = Lane(w.engine, code=None)
    task = asyncio.create_task(run(w, waiter(w, silent, _policy(timeout_s=6.0)), cards3(), query="c"))
    for _ in range(100):
        if found.pending() == 1 and silent.calls:
            break
        await asyncio.sleep(0.05)
    assert found.pending() == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert found.pending() == 0  # cancelled


async def test_the_api_shutdown_closes_the_listen_connection(w: World) -> None:
    from reader.api.app import create_app

    judge_wait = need("reader.search.judge_wait")
    listening = ("SELECT count(*) FROM pg_stat_activity"
                 " WHERE datname = current_database() AND query ILIKE 'LISTEN%'")
    app = create_app(w.settings, w.engine, run_cleanup_task=False)
    async with app.router.lifespan_context(app):
        assert (await run(w, waiter(w, Lane(w.engine)), cards3()))["judge_source"] == "live"
        assert await count(w, listening) == 1
        before = judge_wait.listener(w.engine)
    assert judge_wait.listener(w.engine) is not before  # stop(engine) dropped the process's listener
    for _ in range(40):
        if await count(w, listening) == 0:
            break
        await asyncio.sleep(0.05)
    assert await count(w, listening) == 0


async def test_one_listen_connection_under_twenty_concurrent_finds(w: World) -> None:
    lane = Lane(w.engine)
    judge = waiter(w, lane)
    outs = await asyncio.gather(*(run(w, judge, cards3(), query=f"question {i}") for i in range(20)))
    assert all(o["judge_source"] == "live" for o in outs)
    assert len(lane.calls) == 20 and len({a["request_id"] for _, a in lane.calls}) == 20
    listening = await count(w, "SELECT count(*) FROM pg_stat_activity"
                               " WHERE datname = current_database() AND query ILIKE 'LISTEN%'")
    assert listening == 1


async def test_a_pipeline_card_is_cut_in_the_judge_input_only(w: World) -> None:
    lane = Lane(w.engine)
    long = _card("c0", 0.9, "word " * 400)  # 2,000 characters, over the L1 card budget of 600 tokens
    ctx = dict(tutor_ctx(), line="compile")
    out = await run(w, waiter(w, lane), [long], channel="pipeline", ctx=ctx)
    sent = lane.rows[0]["request"].cards
    assert len(sent[0][1]) <= 1200 and out["cards"][0]["text"] == "word " * 400


# ---- INV-SEARCH-11: one call at most, last, on the slice only ---------------------------------------------


async def test_one_call_at_most_on_a_prefix_slice(w: World) -> None:
    render = need("reader.agent.judges").render_find_input
    estimate = need("reader.search.policy").estimate
    rng = random.Random(11)  # noqa: S311 (test data, not a secret)
    for case in range(12):
        lane = Lane(w.engine)
        corpus = Corpus([_card(f"r{case}-{i}", rng.random(), "x " * rng.randint(1, 700))
                         for i in range(rng.randint(0, 6))])
        budget = rng.choice([400, 450, 700, 1500, 2000])
        out = await run(w, waiter(w, lane), [], corpus=corpus, budget=budget, query=f"case {case}")
        assert corpus.calls == 1 and len(lane.calls) <= 1 and len(lane.rows) <= 1
        if not lane.rows:
            assert out["judge_skip"] == "slice_empty" and out["degraded"] is False
            continue
        sent = list(lane.rows[0]["request"].cards)
        scores = {c.id: c.score for c in corpus.cards}
        assert [scores[i] for i, _ in sent] == sorted((scores[i] for i, _ in sent), reverse=True)
        assert estimate(render(f"case {case}", sent)) <= min(1.5 * budget, 3000)


async def test_an_empty_slice_makes_no_row_no_job_and_no_call(w: World) -> None:
    lane = Lane(w.engine)
    assert (await run(w, waiter(w, lane), []))["judge_skip"] == "slice_empty"
    huge = [_card("c0", 0.9, "z" * 1199)]
    out = await run(w, waiter(w, lane), huge, query="q" * 999, budget=400)
    assert (out["judge_skip"], out["degraded"]) == ("slice_empty", False)
    assert lane.calls == [] and await count(w, "SELECT count(*) FROM judge_requests") == 0


# ---- INV-SEARCH-14: no text in job args -------------------------------------------------------------------


async def test_no_text_in_judge_job_args_or_keys(w: World, pg: Pg) -> None:
    from reader.agent import lane as lane_mod
    from reader.config import Secret
    from reader.db import DbTarget
    from reader.jobs.catalog import Catalog
    from reader.jobs.queue import Jobs
    from reader.jobs.worker import make_app

    canary = "kanareyka" + uuid.uuid4().hex[:8]
    app = make_app(DbTarget(pg.host, pg.port, pg.user, pg.database, Secret(pg.password)))
    await app.open_async()
    try:
        lane = Lane(w.engine, jobs=Jobs(w.engine, app, Catalog(lane_mod.JOB_KINDS)))
        cards = [_card("c0", 0.9, f"text with {canary} inside"), _card("c1", 0.5, "plain")]
        out = await run(w, waiter(w, lane), cards, query=f"what is {canary}")
    finally:
        await app.close_async()
    assert out["judge_source"] == "live"
    assert canary in lane.rows[0]["request"].query and canary in lane.rows[0]["request"].cards[0][1]
    async with w.engine.connect() as conn:
        jobs = [str(r[0]) for r in await conn.execute(text("SELECT args::text FROM procrastinate_jobs"))]
        keys = [str(r) for r in await conn.execute(text("SELECT * FROM job_keys"))]
    assert jobs and keys
    assert not any(canary in x for x in jobs + keys)


# ---- INV-SEARCH-4: deterministic responses ----------------------------------------------------------------


async def test_two_judge_free_finds_are_equal(w: World) -> None:
    gated = _policy(mode="never_judge")
    cards = [_card(f"d{i}", 0.5, "same text") for i in range(6)]  # equal scores: ties by id
    first = await run(w, None, cards, policy=gated)
    second = await run(w, None, cards, policy=gated)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert [c["id"] for c in first["cards"]] == sorted(c["id"] for c in first["cards"])


# ---- INV-SEARCH-12 (judge-request part) and INV-SEARCH-1 over the real corpus ------------------------------


async def test_a_second_user_gets_no_card_and_no_judge_request(
    w: World, embed_server: EmbedServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("READER_EMBED_URL", embed_server.url)
    corpus_mod = need("reader.search.corpus")
    spans = [(1, "Wallet keys sign every transfer of the owner here.")]
    ingest = await add_material(w, "ivan", MAT_EN, spans, config_id="book-en")
    await embed_material(w, ingest, MAT_EN)
    policy = _policy()
    lane = Lane(w.engine)
    for channel in ("tutor", "owner", "pipeline"):
        async with w.engine.connect() as conn:
            out = await run(w, waiter(w, lane, policy), [], channel=channel, user="anna", query="wallet keys",
                            ctx=tutor_ctx(), corpus=corpus_mod.StoreCorpus(conn, policy))
        assert out["cards"] == [] and out["judge_skip"] == "slice_empty"
    assert lane.calls == []
    async with w.engine.connect() as conn:
        mine = await run(w, waiter(w, lane, policy), [], channel="tutor", query="wallet keys",
                         ctx=tutor_ctx(), corpus=corpus_mod.StoreCorpus(conn, policy))
    assert mine["judge_source"] == "live" and mine["cards"][0]["layer"] == "L1"
    request = lane.rows[0]["request"]
    assert request.owner_id == w.users["ivan"] and "Wallet keys" in request.cards[0][1]
