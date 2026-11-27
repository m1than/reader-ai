"""SRV-SEARCH [unit]: the response budget and the count are never exceeded (Hypothesis over fake ports).

@trace INV-SEARCH-9
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
