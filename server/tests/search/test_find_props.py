"""SRV-SEARCH [unit]: the response budget and the count are never exceeded (Hypothesis over fake ports).

@trace INV-SEARCH-9
@trace FR-SEARCH-29
"""

from __future__ import annotations

import asyncio
import math

from hypothesis import given, settings
from hypothesis import strategies as st

from .test_find_unit import USER, Corpus, Judge, _card, _estimate, _find, _policy

SKIPS = ("timeout", "error", "budget", "saturated", "quota_refused", "no_provider")


@settings(max_examples=120, deadline=None)
@given(
    texts=st.lists(st.text(alphabet="ab кл\n\"\\", min_size=0, max_size=1500), max_size=25),
    channel=st.sampled_from(("mcp", "owner", "tutor", "pipeline")),
    budget=st.one_of(st.none(), st.integers(400, 200_000)),
    k=st.one_of(st.none(), st.integers(1, 50)),
    outcome=st.one_of(st.none(), st.sampled_from(SKIPS), st.just("judged")),
    degraded=st.booleans(),
    kept_seed=st.integers(0, 3),
)
def test_response_estimate_within_the_effective_budget(
    texts: list[str], channel: str, budget: int | None, k: int | None, outcome: str | None, degraded: bool,
    kept_seed: int,
) -> None:
    find = _find()
    gate = {"mode": "never_judge" if outcome is None else "always_judge", "margin": None}
    policy = _policy(gate=gate)
    cards = [_card(f"c{i:02d}", 1 - i / 50, t, covers=[f"e{i % 7}"]) for i, t in enumerate(texts)]
    ids = [c.id for c in cards]
    judged = outcome == "judged"
    judge = None if outcome is None else Judge(
        kept=ids[kept_seed::2] if judged else None, skip=None if judged else outcome)
    deps = find.Deps(corpus=Corpus(cards, degraded=degraded), judge=judge, policy=policy,
                     policy_version=lambda: "p" * 64)
    caller = find.Caller(user_id=USER, roles=("owner",), provider=True)
    out = asyncio.run(find.find(caller, channel, "a query", k=k, budget=budget, deps=deps))
    maximum = policy.data["channels"][channel]["budget_max"]
    assert out["budget"] <= maximum and (budget is None or out["budget"] == min(budget, maximum))
    assert _estimate(out) <= out["budget"]
    assert k is None or len(out["cards"]) <= k
    l1 = policy.data["card_budgets"]["L1"]
    for card in out["cards"]:
        if channel != "pipeline":  # an entitled pipeline caller's L1 text is not cut
            assert math.ceil(len(card["text"]) / 2) <= l1


class RawJudge:
    """A judge port returning an unsanitised outcome: repeated, foreign ids, free-form skips and sources."""

    def __init__(self, kept: list[str] | None, skip: str | None, source: str | None) -> None:
        self.kept, self.skip, self.source = kept, skip, source

    async def __call__(self, call: object) -> object:
        return _find().JudgeOutcome(kept=None if self.kept is None else tuple(self.kept), source=self.source,
                                    skip=self.skip)


@settings(max_examples=150, deadline=None)
@given(
    n=st.integers(1, 8),
    kept=st.one_of(st.none(), st.lists(st.sampled_from(["c00", "c01", "c02", "zz", "c00"]), max_size=8)),
    skip=st.one_of(st.none(), st.sampled_from([*SKIPS, "gate", "slice_empty"]), st.text(max_size=12)),
    source=st.one_of(st.none(), st.sampled_from(["live", "recorded"]), st.text(max_size=12)),
)
def test_a_raw_judge_outcome_is_sanitised(n: int, kept: list[str] | None, skip: str | None,
                                          source: str | None) -> None:
    find = _find()
    policy = _policy(gate={"mode": "always_judge", "margin": None})
    cards = [_card(f"c{i:02d}", 1 - i / 10, "x", covers=[f"e{i}"]) for i in range(n)]
    deps = find.Deps(corpus=Corpus(cards), judge=RawJudge(kept, skip, source), policy=policy,
                     policy_version=lambda: "p" * 64)
    caller = find.Caller(user_id=USER, roles=("owner",), provider=True)
    out = asyncio.run(find.find(caller, "owner", "a query", deps=deps))
    ids = [c["id"] for c in out["cards"]]
    assert len(ids) == len(set(ids)) and set(ids) <= {c.id for c in cards}
    closed = {*SKIPS, "gate", "slice_empty"}
    if out["judge_ran"]:
        assert out["judge_source"] in ("live", "recorded") and kept is not None
        assert out["judge_source"] == source
    else:
        assert out["judge_skip"] in closed
        if kept is None and skip not in closed:
            assert out["judge_skip"] == "error" and out["degraded"]
