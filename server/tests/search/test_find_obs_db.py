"""SRV-SEARCH [unit, db]: the text-free `find` subtree (FR-SEARCH-33 over SRV-OBS FR-OBS-36's helpers) for a
gated, a judged and a degraded find through the in-memory exporter, and `cards.read` for SRV-OBS FR-OBS-39/40.

@trace FR-SEARCH-33
"""

from __future__ import annotations

import asyncio
import copy
import logging
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from obs.conftest import finished, obs_setup

from .conftest import MAT_EN, Pg, World, add_material, need, world

BRAIN = "b" * 64
VERSION = "e" * 64
CANARY = "kanareyka" + uuid.uuid4().hex[:8]


def _policy(mode: str) -> Any:
    policy = need("reader.search.policy")
    loaded = policy.load()
    data = copy.deepcopy(dict(loaded.data))
    data["gate"] = {"mode": mode, "margin": None}
    return policy.Policy(data, loaded.facets_sha256)


def _card(card_id: str, score: float, body: str) -> Any:
    cards = need("reader.search.cards")
    return cards.Card(
        id=card_id, layer="L1", title="", text=body, cite=(cards.Cite(spn=card_id, quote_sha256="q" * 64),),
        source=cards.Source(id="s" * 64, role=None, as_of=None, reliability=None, license="personal-use"),
        score=score, pack_id="c" * 64,
    )


CARDS = [_card(f"spn:{'d' * 64}:p{i}:0-9", 0.9 - i / 10, f"text {i} with {CANARY}") for i in range(30)]


@dataclass
class Corpus:
    degraded: bool = False

    async def candidates(self, caller: Any, channel: str, route: Any, query: str, filters: Any,
                         identifiers: Sequence[str]) -> Any:
        find = need("reader.search.find")
        return find.Candidates(cards=list(CARDS), degraded=self.degraded, brain_version=BRAIN,
                               lexemes=frozenset(), layers=frozenset({"L1"}))

    async def neighbours(self, caller: Any, channel: str, cards: Sequence[Any]) -> Mapping[str, list[Any]]:
        return {}


class Judge:
    async def __call__(self, call: Any) -> Any:
        return need("reader.search.find").JudgeOutcome(kept=(call.cards[0][0],), source="live", skip=None)


def _traced(corpus: Any, policy: Any, judge: Any) -> tuple[dict[str, Any], list[Any]]:
    find = need("reader.search.find")
    routes = frozenset(need("reader.search.policy").find_routes())
    with obs_setup("api", find_routes=routes) as (handle, memory):
        deps = find.Deps(corpus=corpus, judge=judge, policy=policy, policy_version=lambda: VERSION)
        caller = find.Caller(uuid.UUID(int=7), ("owner",), True)
        out: dict[str, Any] = asyncio.run(find.find(caller, "owner", f"what about {CANARY}", deps=deps))
        spans = finished(handle, memory)
    return out, spans


def _by_name(spans: list[Any]) -> dict[str, Any]:
    return {s.name: s for s in spans}


def _no_canary(spans: list[Any]) -> None:
    for span in spans:
        assert CANARY not in repr(dict(span.attributes or {})), span.name


def test_the_gated_find_subtree(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    out, spans = _traced(Corpus(), _policy("never_judge"), None)
    named = _by_name(spans)
    assert set(named) == {"find", "find.route", "find.retrieve", "find.preselect", "find.select"}
    root = named["find"]
    assert all(s.parent is not None and s.parent.span_id == root.context.span_id
               for s in spans if s.name != "find")
    attrs = dict(root.attributes or {})
    assert attrs["reader.find.channel"] == "owner" and attrs["reader.find.question_type"] == "thematic"
    assert attrs["reader.brain_version"] == BRAIN and attrs["reader.policy_version"] == VERSION
    assert attrs["reader.judge_ran"] is False and attrs["reader.judge_skip"] == "gate"
    assert attrs["reader.find.cards"] == len(out["cards"])
    assert dict(named["find.route"].attributes or {})["reader.find.route"] == "owner.thematic"
    assert dict(named["find.preselect"].attributes or {})["reader.find.gate"] == "separated"
    retrieve = dict(named["find.retrieve"].attributes or {})
    assert retrieve["reader.retrieval.candidates"] == 30 and "reader.doc.24.uri" in retrieve
    assert "reader.doc.25.uri" not in retrieve  # at most 25 documents per span
    select = dict(named["find.select"].attributes or {})
    assert select["reader.doc.0.uri"] == f"reader://{out['cards'][0]['id']}"
    assert select["reader.doc.0.judged"] is False
    _no_canary(spans)
    assert CANARY not in caplog.text


def test_the_judged_find_subtree() -> None:
    out, spans = _traced(Corpus(), _policy("always_judge"), Judge())
    named = _by_name(spans)
    wait = dict(named["find.judge_wait"].attributes or {})
    assert len(wait["reader.judge.input_sha256"]) == 64 and wait["reader.judge.input_tokens"] > 0
    assert wait["reader.judge.kept"] == 1 and wait["reader.judge_ran"] is True
    root = dict(named["find"].attributes or {})
    assert (root["reader.judge_ran"], root["reader.judge_source"]) == (True, "live")
    assert dict(named["find.preselect"].attributes or {})["reader.find.gate"] == "not_separated"
    select = dict(named["find.select"].attributes or {})
    assert select["reader.doc.0.judged"] is True and select["reader.find.judged_tokens"] > 0
    assert out["judge_source"] == "live"
    _no_canary(spans)


@dataclass
class MixedCorpus:
    """One shareable card of a pack the caller is not entitled to among entitled ones."""

    async def candidates(self, caller: Any, channel: str, route: Any, query: str, filters: Any,
                         identifiers: Sequence[str]) -> Any:
        from dataclasses import replace

        find = need("reader.search.find")
        foreign = replace(CARDS[0], entitled=False, source=replace(CARDS[0].source, license="shareable"))
        return find.Candidates(cards=[foreign, *CARDS[1:3]], degraded=False, brain_version=BRAIN,
                               lexemes=frozenset(), layers=frozenset({"L1"}))

    async def neighbours(self, caller: Any, channel: str, cards: Sequence[Any]) -> Mapping[str, list[Any]]:
        return {}


def test_the_find_span_records_entitled_both_ways() -> None:
    _, spans = _traced(Corpus(), _policy("never_judge"), None)
    assert dict(_by_name(spans)["find"].attributes or {})["reader.find.entitled"] is True
    out, spans = _traced(MixedCorpus(), _policy("never_judge"), None)
    assert CARDS[0].id in {c["id"] for c in out["cards"]}  # the non-entitled shareable card is served
    assert dict(_by_name(spans)["find"].attributes or {})["reader.find.entitled"] is False


def test_the_degraded_find_subtree() -> None:
    out, spans = _traced(Corpus(degraded=True), _policy("always_judge"), None)
    root = dict(_by_name(spans)["find"].attributes or {})
    assert out["degraded"] is True and root["reader.judge_skip"] == "error"
    assert "find.judge_wait" not in _by_name(spans)  # the waiter was never called
    _no_canary(spans)


# ---- cards.read [db] ---------------------------------------------------------------------------------------


@pytest.fixture
async def w(pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    built = await world(pg, tmp_path, monkeypatch)
    try:
        yield built
    finally:
        await built.engine.dispose()


@pytest.mark.anyio
async def test_cards_read_applies_the_callers_row(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from reader.ingest import views

    cards, find = need("reader.search.cards"), need("reader.search.find")
    opening = need("reader.search.opening")
    await add_material(w, "ivan", MAT_EN, [(1, "First span text."), (2, "Second span text.")],
                       config_id="book-en", outline=[(1, "Chapter", 1)])
    async with w.engine.connect() as conn:
        pack = await views.active_pack(conn, w.users["ivan"], MAT_EN)
        assert pack is not None
        span = next(s.locator for s in await views.pack_spans(conn, pack) if s.locator)
        node = f"sec:{MAT_EN}:1"
        caller = find.Caller(w.users["ivan"], ("owner",), True)
        mine = await cards.read(conn, caller, [span, node, "spn:unknown"])
        assert mine[span] == "First span text." and node in mine and "spn:unknown" not in mine
        other = await cards.read(conn, find.Caller(w.users["anna"], ("learner",), True), [span, node])
        assert other == {}  # INV-SEARCH-1: not in anna's manifest

        async def not_entitled(*_: Any) -> bool:
            return False

        monkeypatch.setattr(opening, "entitled", not_entitled)
        mcp_row = await cards.read(conn, caller, [span, node])
        assert span not in mcp_row and node in mcp_row  # the MCP row: no L1 card for a non-entitled pack
