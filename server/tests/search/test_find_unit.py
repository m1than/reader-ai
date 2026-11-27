"""SRV-SEARCH [unit]: the find cascade over fake ports (entry, routing, gate, expansion, step 7, card shape).

@trace FR-SEARCH-22 FR-SEARCH-23 FR-SEARCH-24 FR-SEARCH-29 INV-SEARCH-15 VEC-SEARCH-8 VEC-SEARCH-10
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from .conftest import fixture, need

USER = uuid.UUID(int=1)
BRAIN = "b" * 64
POLICY_V = "p" * 64
LICENSE = "personal-use"


def _find() -> Any:
    return need("reader.search.find")


def _cards() -> Any:
    return need("reader.search.cards")


def _policy(**changes: Any) -> Any:
    policy = need("reader.search.policy")
    data = copy.deepcopy(dict(policy.load().data))
    for path, value in changes.items():
        node = data
        keys = path.split("__")
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = value
    return policy.Policy(data, "f" * 64)


def _card(card_id: str, score: float, text: str = "", covers: Sequence[str] = (), layer: str = "L1",
          nxt: Sequence[str] = (), title: str = "") -> Any:
    cards = _cards()
    return cards.Card(
        id=card_id, layer=layer, title=title, text=text,
        cite=(cards.Cite(spn=card_id, quote_sha256="q" * 64),),
        source=cards.Source(id="s" * 64, role=None, as_of=None, reliability=None, license=LICENSE),
        next=tuple(nxt), score=score, covers=frozenset(covers),
    )


@dataclass
class Corpus:
    cards: list[Any]
    neighbours_of: Mapping[str, list[Any]] = field(default_factory=dict)
    degraded: bool = False
    reads: int = 0
    routes: list[Any] = field(default_factory=list)

    async def candidates(self, caller: Any, channel: str, route: Any, query: str, filters: Any,
                         identifiers: Sequence[str]) -> Any:
        self.reads += 1
        self.routes.append(route)
        return _find().Candidates(cards=list(self.cards), degraded=self.degraded, brain_version=BRAIN,
                                  lexemes=frozenset({"a"}), layers=frozenset({"L1"}))

    async def neighbours(self, caller: Any, channel: str, cards: Sequence[Any]) -> Mapping[str, list[Any]]:
        return {c.id: list(self.neighbours_of.get(c.id, [])) for c in cards}


@dataclass
class Judge:
    kept: Sequence[str] | None = None
    skip: str | None = None
    calls: list[Any] = field(default_factory=list)

    async def __call__(self, call: Any) -> Any:
        self.calls.append(call)
        if self.skip is not None:
            return _find().JudgeOutcome(kept=None, source=None, skip=self.skip)
        ids = [i for i, _ in call.cards]
        kept = [i for i in (self.kept if self.kept is not None else ids) if i in ids]
        return _find().JudgeOutcome(kept=tuple(kept), source="live", skip=None)


def _run(corpus: Corpus, *, policy: Any = None, judge: Any = None, channel: str = "owner",
         query: str = "a query", **kw: Any) -> dict[str, Any]:
    find = _find()
    deps = find.Deps(corpus=corpus, judge=judge, policy=policy or _policy(), policy_version=lambda: POLICY_V)
    caller = find.Caller(user_id=USER, roles=("owner",), provider=True)
    result: dict[str, Any] = asyncio.run(find.find(caller, channel, query, deps=deps, **kw))
    return result


def _estimate(value: Any) -> int:
    return math.ceil(len(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)) / 2)


# ---- FR-SEARCH-22: entry, budget and response -------------------------------------------------------------

@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"query": "   "}, "query_invalid"),
        ({"query": "x" * 1001}, "query_invalid"),
        ({"query": "a\x00b"}, "query_invalid"),
        ({"query": "a\ud800b"}, "query_invalid"),
        ({"query": "a\udfff"}, "query_invalid"),
        ({"k": 0}, "k_invalid"),
        ({"k": 51}, "k_invalid"),
        ({"budget": 399}, "budget_invalid"),
        ({"filters": {"colour": "red"}}, "filter_invalid"),
        ({"filters": {"role": "bogus"}}, "filter_invalid"),
        ({"channel": "web"}, "channel_invalid"),
    ],
)
def test_each_validation_code(kw: dict[str, Any], code: str) -> None:
    with pytest.raises(_find().FindError) as caught:
        _run(Corpus([_card("a", 0.9, "x")]), **kw)
    assert caught.value.code == code


def test_budget_min_is_accepted_and_a_budget_above_max_is_clamped() -> None:
    assert _run(Corpus([]), budget=400)["budget"] == 400
    assert _run(Corpus([]), channel="mcp", budget=999_999)["budget"] == 2000
    assert _run(Corpus([]), channel="tutor")["budget"] == 5250
    assert _run(Corpus([]))["budget"] == 2000


def test_query_is_nfc_and_trimmed() -> None:
    corpus = Corpus([])
    _run(corpus, query="  Café  ")
    find = _find()
    assert find.normalise_query("  Café  ") == "Café"


def test_response_shape_and_degraded_rules() -> None:
    gate = _run(Corpus([_card("a", 0.9, "alpha")]))
    assert set(gate) == {
        "brain_version", "policy_version", "budget", "cards", "judge_ran", "judge_skip", "degraded"}
    assert (gate["brain_version"], gate["policy_version"]) == (BRAIN, POLICY_V)
    assert gate["judge_skip"] == "gate" and gate["degraded"] is False and gate["judge_ran"] is False
    assert _run(Corpus([_card("a", 0.9, "alpha")], degraded=True))["degraded"] is True
    judged = _policy(gate={"mode": "always_judge", "margin": None})
    for skip in ("timeout", "error", "budget", "saturated", "quota_refused", "no_provider"):
        out = _run(Corpus([_card("a", 0.9, "alpha")]), policy=judged, judge=Judge(skip=skip))
        assert (out["degraded"], out["judge_skip"], out["judge_ran"]) == (True, skip, False)
    out = _run(Corpus([_card("a", 0.9, "alpha")]), policy=judged, judge=Judge())
    assert (out["degraded"], out["judge_ran"], out["judge_source"]) == (False, True, "live")
    assert "judge_skip" not in out
    empty = _run(Corpus([]), policy=judged, judge=Judge())
    assert (empty["judge_skip"], empty["degraded"]) == ("slice_empty", False)


def test_slice_limit_counts_the_query_in_the_rendered_input() -> None:
    """Code review of fb7b926 (blocking 2): the slice limit is min(1.5 x B, cap) on the rendered input, which
    includes the query; one full personal-use L1 card whose cost fits the preselection but whose rendering
    with a 999-character query passes 1.5 x B is not sent."""
    sha = "a" * 64
    body = ("word " * 240).strip()  # 1,199 characters
    spn = f"spn:{sha}:p1:0-1199"
    judge = Judge()
    judged = _policy(gate={"mode": "always_judge", "margin": None})
    out = _run(Corpus([_card(spn, 0.9, body)]), policy=judged, judge=judge, query=("why " * 250).strip(),
               budget=740)
    assert judge.calls == []
    assert (out["judge_skip"], out["degraded"], out["judge_ran"]) == ("slice_empty", False, False)
    no_lane = _run(Corpus([_card("a", 0.9, "alpha")]), policy=judged, judge=None)
    assert (no_lane["judge_skip"], no_lane["degraded"]) == ("error", True)


def test_the_whole_response_fits_the_budget_at_the_boundary() -> None:
    cards = [_card(f"c{i:02d}", 1 - i / 100, "w" * 20 + f" {i}", covers=[f"e{i}"]) for i in range(40)]
    for budget in (400, 401, 555, 1000, 2000):
        out = _run(Corpus(cards), budget=budget)
        assert _estimate(out) <= budget
        assert out["cards"], budget  # the cards' budget is used, not left empty
        one_more = _estimate(out) + _cards().cost(_card("c99", 0.0, "w" * 20 + " 99", covers=["e99"]))
        assert one_more > budget - 1 or len(out["cards"]) == len(cards)  # the budget binds near its limit


def test_budget_min_holds_the_envelope_and_the_smallest_card() -> None:
    find, cards = _find(), _cards()
    policy = _policy()
    envelope = find._envelope("b" * 64, "p" * 64, 150000)
    smallest = _card("sec:" + "a" * 64, 0.0, "", layer="L2")
    assert _estimate(envelope) + cards.cost(smallest) <= policy.data["budget_min"]


def test_a_cards_cost_is_its_whole_json() -> None:
    cards = _cards()
    card = _card("a", 0.5, "some text ключ")
    body = cards.to_json(card, judged=False)
    assert set(body) == {"id", "layer", "title", "text", "cite", "source", "next", "judged"}
    assert cards.cost(card) == _estimate(body) + 0.5
    assert cards.to_json(card, judged=True)["judged"] is True


def test_cuts_at_whitespace_and_hard() -> None:
    cards = _cards()
    assert cards.cut("aaa bbb ccc", 4) == "aaa bbb"  # 4 tokens = 8 characters, last whitespace at 7
    assert cards.cut("a" * 20, 4) == "a" * 8
    assert cards.cut("short", 4) == "short"


def test_l1_text_budget_and_more() -> None:
    cards = _cards()
    policy = _policy()
    long = ("word " * 400).strip()  # 1,999 characters
    text, more = cards.l1_text(long, policy, "owner", entitled=True, shareable=False)
    assert len(text) <= 1200 and more is False and long.startswith(text)
    text, more = cards.l1_text(long, policy, "owner", entitled=True, shareable=True)
    assert more is True
    text, more = cards.l1_text(long, policy, "pipeline", entitled=True, shareable=False)
    assert text == long and more is False  # pipeline, entitled: not cut
    text, more = cards.l1_text("short", policy, "owner", entitled=True, shareable=True)
    assert (text, more) == ("short", False)


def test_l2_and_l0_cut_order() -> None:
    cards = _cards()
    policy = _policy()
    assert cards.l2_budget(policy, has_children=False, top_level=False) == 100
    assert cards.l2_budget(policy, has_children=True, top_level=False) == 300
    assert cards.l2_budget(policy, has_children=True, top_level=True) == 500
    quote = "q" * 50 + " " + "r" * 249  # 300 characters
    text, served = cards.l2_text("Chapter 1 / Keys", None, quote, 100)  # 200 characters
    assert text == "Chapter 1 / Keys" and served == "q" * 50  # the quote takes the rest, cut at whitespace
    text, served = cards.l2_text("T " * 150, None, quote, 100)  # the title path alone exceeds the budget
    assert len(text) <= 200 and served is None
    text, served = cards.l2_text("x" * 199, None, quote, 100)  # 1 character left: nothing at whitespace
    assert served is None or len(served) <= 1
    text, served = cards.l2_text("Title", "A summary of it.", quote, 100)
    assert text == "Title\nA summary of it." and served is not None and len(text) + len(served) <= 200
    assert cards.cut("x" * 500, policy.data["card_budgets"]["L0"]) == "x" * 400


def test_request_hash_changes_with_each_field() -> None:
    find = _find()
    base = {"user_id": str(USER), "roles": ["owner"], "channel": "owner", "query": "q", "filters": {},
            "k": None, "budget": 2000, "brain_version": BRAIN, "policy_version": POLICY_V}
    digest = find.request_hash(**base)
    canonical = json.dumps(base, sort_keys=True, separators=(",", ":")).encode()
    assert digest == hashlib.sha256(canonical).hexdigest()
    for key, other in (("user_id", str(uuid.UUID(int=2))), ("roles", ["learner"]), ("channel", "mcp"),
                       ("query", "r"), ("filters", {"role": "map"}), ("k", 3), ("budget", 1999),
                       ("brain_version", "c" * 64), ("policy_version", "d" * 64)):
        assert find.request_hash(**{**base, key: other}) != digest, key


def test_no_cache_two_calls_read_twice() -> None:
    corpus = Corpus([_card("a", 0.9, "alpha")])
    first = _run(corpus)
    second = _run(corpus)
    assert corpus.reads == 2 and first == second


# ---- FR-SEARCH-23, VEC-SEARCH-8 ----

@pytest.mark.parametrize("case", fixture("routes.yaml")["cases"], ids=lambda c: c["q"][:30])
def test_classification_and_route_vec_search_8(case: dict[str, Any]) -> None:
    find = _find()
    kind, identifiers = find.classify(_policy(), case["q"], case.get("question_type"),
                                      case.get("channel", "owner"))
    assert kind == case["type"] and list(identifiers) == case["identifiers"]
    for channel in ([case["channel"]] if "channel" in case else ["mcp", "owner", "tutor", "pipeline"]):
        corpus = Corpus([])
        _run(corpus, channel=channel, query=case["q"], question_type=case.get("question_type"))
        assert corpus.routes[0].id == f"{channel}.{case['type']}"


@pytest.mark.parametrize("case", fixture("routes.yaml")["refused"], ids=lambda c: f"{c['channel']}")
def test_question_type_only_from_the_pipeline(case: dict[str, Any]) -> None:
    with pytest.raises(_find().FindError) as caught:
        _run(Corpus([]), channel=case["channel"], query=case["q"], question_type=case["question_type"])
    assert caught.value.code == case["code"]


def test_unbuilt_layers_are_skipped() -> None:
    policy = _policy()
    policy.data["routes"]["owner.thematic"]["layers"] = ["L3", "L1", "L2"]
    corpus = Corpus([])
    _run(corpus, policy=policy)
    assert corpus.routes[0].layers == ("L1", "L2") and corpus.routes[0].skipped == ("L3",)


# ---- FR-SEARCH-24: gate, expansion, preselection ----------------------------------------------------------

def test_gate_modes_and_the_margin_boundary() -> None:
    find = _find()
    assert find.separated(_policy(), [0.9, 0.1]) is True
    always = _policy(gate={"mode": "always_judge", "margin": None})
    assert find.separated(always, [0.9, 0.1]) is False
    margin = _policy(gate={"mode": "margin", "margin": 0.25})
    assert find.separated(margin, [0.5]) is True
    assert find.separated(margin, []) is True
    assert find.separated(margin, [0.75, 0.5]) is True  # s1 - s2 = margin
    assert find.separated(margin, [0.74, 0.5]) is False


def test_expansion_scores_cap_and_layers() -> None:
    find = _find()
    policy = _policy()
    a, b = _card("a", 0.8, nxt=["n1", "a2"]), _card("b", 0.6, nxt=["n1"])
    n1, a2 = _card("n1", 0.0), _card("a2", 0.0)
    also = _card("b2", 0.0)
    section = _card("sec", 0.0, layer="L2")
    neighbours = {"a": [n1, a2, section], "b": [n1, also]}
    expanded = find.expand([a, b], neighbours, policy, ("L1",))
    scores = {c.id: c.score for c in expanded}
    assert scores["n1"] == pytest.approx(0.4)  # max over parents: 0.8 * 0.5
    assert scores["a2"] == pytest.approx(0.4) and scores["b2"] == pytest.approx(0.3)
    assert "sec" not in scores  # an L2 target on an L1-only route
    own = find.expand([a, _card("n1", 0.7)], {"a": [n1], "n1": []}, policy, ("L1",))
    assert {c.id: c.score for c in own}["n1"] == pytest.approx(0.7)  # its own score is higher
    many = [_card(f"m{i:02d}", 0.5) for i in range(60)]
    capped = find.expand(many, {}, policy, ("L1",))
    assert [c.id for c in capped] == [f"m{i:02d}" for i in range(50)]  # top 50 by (score desc, id)
    per_card = find.expand([a], {"a": [n1, a2, also]}, policy, ("L1",))
    assert {c.id for c in per_card} == {"a", "n1", "a2"}  # expand_per_card 2


def test_preselection_cost_within_the_factor() -> None:
    policy = _policy(gate={"mode": "always_judge", "margin": None})
    cards = [_card(f"c{i:02d}", 1 - i / 100, "w" * 300 + f" {i}", covers=[f"e{i}"]) for i in range(40)]
    judge = Judge()
    out = _run(Corpus(cards), policy=policy, judge=judge, budget=1000)
    assert judge.calls and _estimate(out) <= 1000
    call = judge.calls[0]
    assert call.preselection_cost <= 1.5 * call.cards_budget + 1e-9


def test_links_merge_by_pivot() -> None:
    find = _find()
    a, b, c = _card("a", 0.9, "A"), _card("b", 0.8, "B"), _card("c", 0.7, "C")
    merged = find.merge([a, b, c], lambda x, y: {x, y} == {"a", "c"}, seed="s1")
    by_id = {m.id: m for m in merged}
    pivot = "a" if "a" in by_id else "c"
    assert len(merged) == 2 and by_id[pivot].score == pytest.approx(0.9)
    assert {x.spn for x in by_id[pivot].cite} == {"a", "c"}


# ---- FR-SEARCH-29, VEC-SEARCH-10 ----

def _vec10() -> dict[str, Any]:
    data: dict[str, Any] = fixture("judge-mixed.yaml")
    return data


@pytest.mark.parametrize("case", _vec10()["steps"], ids=lambda c: c["name"])
def test_step_seven_vec_search_10(case: dict[str, Any]) -> None:
    data = _vec10()
    find = _find()
    greedy = need("reader.search.greedy")
    cards = data["cards"]
    covers = {c: frozenset(v["covers"]) for c, v in cards.items()}
    f = greedy.Coverage(dict.fromkeys(data["elements"], 1.0), covers)
    cost = {c: v["cost"] for c, v in cards.items()}
    score = {c: v["score"] for c, v in cards.items()}
    picked = find.step7(
        f, cost, score, case["budget"], case.get("k"), pool=case.get("pool", case.get("preselection")),
        preselection=case.get("preselection", []), judged_slice=case.get("slice", []), kept=case["kept"],
    )
    assert [c for c, _ in picked] == case["expect"]
    assert [j for _, j in picked] == case["judged"]


@pytest.mark.parametrize("case", _vec10()["slices"], ids=lambda c: c["name"])
def test_judge_slice_vec_search_10(case: dict[str, Any]) -> None:
    find = _find()
    cards = [(c, "x" * 100) for c in case["cards"]]
    limit = min(1.5 * case["budget"], case["cap"])
    assert find.judge_slice("q", cards, limit) == case["expect"]


def test_degraded_judge_selects_from_the_capped_expanded_set() -> None:
    policy = _policy(gate={"mode": "always_judge", "margin": None})
    a = _card("a", 0.9, "alpha", covers=["e1"], nxt=["n"])
    n = _card("n", 0.0, "next one", covers=["e2"])
    out = _run(Corpus([a], neighbours_of={"a": [n]}), policy=policy, judge=Judge(skip="timeout"))
    assert [c["id"] for c in out["cards"]] == ["a", "n"] and not any(c["judged"] for c in out["cards"])


def test_judged_response_orders_kept_first() -> None:
    policy = _policy(gate={"mode": "always_judge", "margin": None})
    cards = [_card(x, s, x * 5, covers=[x]) for x, s in (("a", 0.9), ("b", 0.8), ("c", 0.7))]
    out = _run(Corpus(cards), policy=policy, judge=Judge(kept=["c", "a"]))
    # b dropped, no remainder
    assert [(c["id"], c["judged"]) for c in out["cards"]] == [("c", True), ("a", True)]


# ---- INV-SEARCH-15 ----

def test_unreviewed_findings_are_never_returned() -> None:
    find = _find()
    policy = _policy()
    policy.data["routes"]["owner.thematic"]["layers"] = ["L1", "L4"]
    good = _card("fnd:good", 0.8, "a reviewed finding", layer="L4", covers=["e1"])
    bad = _card("fnd:bad", 0.9, "an unreviewed finding", layer="L4", covers=["e2"])

    class Findings:
        async def for_query(self, caller: Any, channel: str, query: str) -> list[Any]:
            return [find.Finding(card=good, reviewed=True), find.Finding(card=bad, reviewed=False)]

    deps = find.Deps(corpus=Corpus([]), judge=None, policy=policy, policy_version=lambda: POLICY_V,
                     findings=Findings())
    caller = find.Caller(user_id=USER, roles=("owner",), provider=True)
    out = asyncio.run(find.find(caller, "owner", "q", deps=deps))
    assert [c["id"] for c in out["cards"]] == ["fnd:good"]


# ---- code review of f573e0c: step 7 inside find, preselection without k, the copyright guard ----


@dataclass
class LexCorpus(Corpus):
    lexemes: frozenset[str] = frozenset()

    async def candidates(self, caller: Any, channel: str, route: Any, query: str, filters: Any,
                         identifiers: Sequence[str]) -> Any:
        return _find().Candidates(cards=list(self.cards), degraded=False, brain_version=BRAIN,
                                  lexemes=self.lexemes, layers=frozenset({"L1"}))


def _with(card: Any, *, license_: str = LICENSE, entitled: bool = True) -> Any:
    from dataclasses import replace

    return replace(card, source=replace(card.source, license=license_), entitled=entitled)


def test_pass_two_runs_on_the_marginal_objective_inside_find() -> None:
    """VEC-SEARCH-10's pass-2 step through find: kept A covers y1, y2; R1 adds only y1 (higher score), R2
    adds x4; room for one more card after A: R2."""
    find, cards = _find(), _cards()
    estimate = need("reader.search.policy").estimate
    body = "w " * 100
    a = _card("A", 0.9, body, covers=["l:y1", "l:y2"])
    r1 = _card("R1", 0.8, body, covers=["l:y1"])
    r2 = _card("R2", 0.7, body, covers=["l:x4"])
    from reader.agent.judges import render_find_input

    cap = estimate(render_find_input("a query", [("A", body)]))
    policy = _policy(gate={"mode": "always_judge", "margin": None}, judge__input_cap_tokens=cap,
                     objective__relevance_weight=0.0)
    envelope = estimate(cards.text_of(find._envelope(BRAIN, POLICY_V, 10500)))
    budget = math.ceil(envelope + cards.cost(a) + 1.5 * cards.cost(r2))
    out = _run(LexCorpus([a, r1, r2], lexemes=frozenset({"y1", "y2", "x4"})), policy=policy,
               judge=Judge(kept=["A"]), budget=budget)
    assert [(c["id"], c["judged"]) for c in out["cards"]] == [("A", True), ("R2", False)]


def test_the_preselection_has_no_k() -> None:
    policy = _policy(gate={"mode": "always_judge", "margin": None})
    cards = [_card(x, s, x * 5, covers=[x]) for x, s in (("a", 0.9), ("b", 0.8))]
    out = _run(Corpus(cards), policy=policy, judge=Judge(kept=["b"]), k=1)
    assert [(c["id"], c["judged"]) for c in out["cards"]] == [("b", True)]


@pytest.mark.parametrize(("channel", "entitled"), [("mcp", True), ("owner", False)])
def test_personal_use_l1_never_reaches_the_response_or_the_judge(channel: str, entitled: bool) -> None:
    policy = _policy(gate={"mode": "always_judge", "margin": None})
    hidden = _with(_card("hidden", 0.95, "secret words", covers=["e1"]), entitled=entitled)
    parent = _with(_card("sec:x", 0.9, "Chapter", covers=["e2"], layer="L2", nxt=["target"]),
                   entitled=entitled)
    target = _with(_card("target", 0.1, "target words", covers=["e3"]), entitled=entitled)
    shared = _with(_card("open", 0.5, "shareable words", covers=["e4"]), license_="shareable",
                   entitled=entitled)
    judge = Judge()
    out = _run(Corpus([hidden, parent, shared], neighbours_of={"sec:x": [target]}), policy=policy,
               judge=judge, channel=channel)
    assert {c["id"] for c in out["cards"]} == {"sec:x", "open"}
    assert judge.calls and {i for call in judge.calls for i, _ in call.cards} <= {"sec:x", "open"}


@pytest.mark.parametrize(("channel", "license_", "cut", "more"), [
    ("owner", LICENSE, True, False), ("owner", "shareable", True, True), ("pipeline", LICENSE, False, False)])
def test_long_l1_text_is_cut_by_channel(channel: str, license_: str, cut: bool, more: bool) -> None:
    text = "word " * 400  # 2,000 characters, over the 1,200-character L1 card budget
    out = _run(Corpus([_with(_card("long", 0.9, text, covers=["e1"]), license_=license_)]), channel=channel)
    card = out["cards"][0]
    assert (len(card["text"]) <= 1200 and text.startswith(card["text"])) if cut else card["text"] == text
    assert card.get("more", False) is more


def test_judge_input_cuts_l1_on_the_pipeline_and_the_slice_obeys_the_budget_factor() -> None:
    policy = _policy(gate={"mode": "always_judge", "margin": None})
    text = "word " * 400
    judge = Judge()
    out = _run(Corpus([_card("long", 0.9, text, covers=["e1"])]), policy=policy, judge=judge,
               channel="pipeline")
    assert out["cards"][0]["text"] == text  # the response is uncut on the pipeline
    assert [len(t) <= 1200 for _, t in judge.calls[0].cards] == [True]
    small = Judge()
    many = [_card(f"c{i}", 0.9 - i / 100, "z " * 150, covers=[f"e{i}"]) for i in range(8)]
    _run(Corpus(many), policy=policy, judge=small, budget=400)
    assert 0 < len(small.calls[0].cards) < 8  # 1.5 x the cards' budget binds below the 3,000-token cap


# ---- phase 2 unit 5: the deferred review items ----


def test_the_response_fits_a_six_digit_budget_at_the_boundary() -> None:
    cards = [_with(_card(f"c{i:02d}", 1 - i / 100, "w " * 2300 + str(i), covers=[f"e{i}"]))
             for i in range(50)]
    for budget in (100_000, 100_001, 109_999):
        out = _run(Corpus(cards), channel="pipeline", budget=budget)
        assert out["budget"] == budget and _estimate(out) <= budget
        assert len(out["cards"]) < len(cards)  # the budget binds
        smallest = min(_cards().cost(c) for c in cards)
        assert _estimate(out) + smallest > budget - 1  # filled up to its limit


def test_findings_without_l4_in_the_route_add_no_card() -> None:
    find = _find()
    policy = _policy()
    assert "L4" not in policy.data["routes"]["owner.thematic"]["layers"]
    finding = _card("fnd:good", 0.99, "a reviewed finding", layer="L4", covers=["e1"])

    class Findings:
        async def for_query(self, caller: Any, channel: str, query: str) -> list[Any]:
            return [find.Finding(card=finding, reviewed=True)]

    deps = find.Deps(corpus=Corpus([_card("a", 0.5, "alpha", covers=["e2"])]), judge=None, policy=policy,
                     policy_version=lambda: POLICY_V, findings=Findings())
    caller = find.Caller(user_id=USER, roles=("owner",), provider=True)
    out = asyncio.run(find.find(caller, "owner", "q", deps=deps))
    assert [c["id"] for c in out["cards"]] == ["a"]


def test_the_pivot_is_seeded_with_the_request_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    find = _find()
    seeds: list[str] = []
    real = find.greedy.pivot

    def spy(ids: Any, plus: Any, seed: str) -> Any:
        seeds.append(seed)
        return real(ids, plus, seed)

    monkeypatch.setattr(find.greedy, "pivot", spy)
    _run(Corpus([_card("a", 0.9, "A"), _card("b", 0.8, "B")]), query="seeded", k=2, budget=900)
    expected = find.request_hash(
        user_id=str(USER), roles=["owner"], channel="owner", query="seeded", filters={}, k=2, budget=900,
        brain_version=BRAIN, policy_version=POLICY_V,
    )
    assert seeds == [expected]


def test_a_policy_error_is_a_find_error(monkeypatch: pytest.MonkeyPatch) -> None:
    find, policy = _find(), need("reader.search.policy")

    def broken() -> Any:
        raise policy.PolicyError("policy_invalid")

    monkeypatch.setattr(policy, "current", broken)
    deps = find.Deps(corpus=Corpus([]))
    caller = find.Caller(user_id=USER, roles=("owner",), provider=True)
    with pytest.raises(find.FindError) as caught:
        asyncio.run(find.find(caller, "owner", "q", deps=deps))
    assert caught.value.code == "policy_invalid"
