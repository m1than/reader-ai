"""SRV-SEARCH FR-SEARCH-22..24, -29: the `find` cascade (ADR-0039 B6) over ports.

Step 1 routes by rules, step 2 asks the `Corpus` for candidates, step 3 merges `Links` by pivot, step 4 gates,
step 5 expands one hop and preselects under `preselect_factor` times the cards' budget, step 6 makes at most
one `Judge` call on the top slice, step 7 selects under the cards' budget. Steps 1-5 and 7 make no model call.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final, Protocol

from reader.agent.judges import render_find_input
from reader.search import cards as card_shape
from reader.search import greedy
from reader.search import policy as policy_mod
from reader.search.cards import Card
from reader.search.policy import CHANNELS, Policy, canonical, estimate

QUERY_MAX: Final = 1000
UNBUILT: Final = frozenset({"L3", "L5"})  # FR-SEARCH-23: layers whose specs have not landed
DEGRADING: Final = frozenset({"timeout", "error", "budget", "saturated", "quota_refused", "no_provider"})
SKIPS: Final = DEGRADING | {"gate", "slice_empty"}  # FR-SEARCH-29's closed set
SOURCES: Final = frozenset({"live", "recorded"})
# The envelope's largest form (FR-SEARCH-22): the longest value of each optional key.
_LONGEST_SKIP: Final = "quota_refused"
_LONGEST_SOURCE: Final = "recorded"


class FindError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Caller:
    user_id: uuid.UUID
    roles: tuple[str, ...] = ()
    provider: bool = True


@dataclass(frozen=True)
class Route:
    id: str
    layers: tuple[str, ...]
    skipped: tuple[str, ...]
    filters: Mapping[str, Any]
    rrf: bool
    l5_boost: bool


@dataclass(frozen=True)
class Candidates:
    cards: list[Card]
    degraded: bool
    brain_version: str
    lexemes: frozenset[str]
    layers: frozenset[str]


@dataclass(frozen=True)
class JudgeCall:
    caller: Caller
    channel: str
    query: str
    cards: tuple[tuple[str, str], ...]
    request_hash: str
    judge_ctx: Mapping[str, Any] | None
    pack_ids: tuple[str, ...]
    cards_budget: float
    preselection_cost: float


@dataclass(frozen=True)
class JudgeOutcome:
    kept: tuple[str, ...] | None  # None: no applied output (see skip)
    source: str | None  # live | recorded
    skip: str | None


@dataclass(frozen=True)
class Finding:
    card: Card
    reviewed: bool


class Corpus(Protocol):
    async def candidates(
        self, caller: Caller, channel: str, route: Route, query: str, filters: Mapping[str, Any],
        identifiers: Sequence[str],
    ) -> Candidates: ...

    async def neighbours(
        self, caller: Caller, channel: str, cards: Sequence[Card]
    ) -> Mapping[str, Sequence[Card]]: ...


class Judge(Protocol):
    async def __call__(self, call: JudgeCall) -> JudgeOutcome: ...


class Findings(Protocol):
    async def for_query(self, caller: Caller, channel: str, query: str) -> Sequence[Finding]: ...


def no_links(_a: str, _b: str) -> bool:
    """The production `Links` port until SRV-KB: no same_point links, so step 3 is the identity."""
    return False


@dataclass(frozen=True)
class Deps:
    corpus: Corpus
    judge: Judge | None = None
    policy: Policy | None = None
    policy_version: Callable[[], str] | None = None
    links: Callable[[str, str], bool] = no_links
    findings: Findings | None = None  # None: L4 is not built


@dataclass(frozen=True)
class Request:
    channel: str
    query: str
    filters: Mapping[str, Any]
    k: int | None
    budget: int


def normalise_query(query: str) -> str:
    return unicodedata.normalize("NFC", query).strip()


def _filters_ok(filters: Mapping[str, Any]) -> bool:
    from reader.ingest import facets as ingest_facets

    loaded = ingest_facets.load()
    for key, value in filters.items():
        facet = loaded.facets.get(key)
        if facet is None or not isinstance(value, str):
            return False
        try:
            ingest_facets.check_tag(loaded, key, value)
        except ingest_facets.FacetsError as error:
            if error.code != "facet_disabled":
                return False
    return True


def validate(
    policy: Policy, channel: str, query: str, filters: Mapping[str, Any] | None, k: int | None,
    budget: int | None, question_type: str | None,
) -> Request:
    if channel not in CHANNELS:
        raise FindError("channel_invalid")
    if question_type is not None and (channel != "pipeline" or question_type != "fact_check"):
        raise FindError("channel_invalid")
    text = normalise_query(query) if isinstance(query, str) else ""
    if not 1 <= len(text) <= QUERY_MAX:
        raise FindError("query_invalid")
    found = dict(filters or {})
    if not _filters_ok(found):
        raise FindError("filter_invalid")
    data = policy.data
    if k is not None and (isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= data["k_max"]):
        raise FindError("k_invalid")
    return Request(channel, text, found, k, effective_budget(policy, channel, budget))


def effective_budget(policy: Policy, channel: str, budget: int | None) -> int:
    """FR-SEARCH-22: unset → the channel's default; below budget_min → budget_invalid; above max → clamped."""
    channel_budgets = policy.channel(channel)
    if budget is None:
        return channel_budgets["budget_default"]
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < policy.data["budget_min"]:
        raise FindError("budget_invalid")
    return min(budget, channel_budgets["budget_max"])


def classify(
    policy: Policy, query: str, question_type: str | None, channel: str
) -> tuple[str, tuple[str, ...]]:
    """FR-SEARCH-23: (question type, identifiers in order of first match)."""
    rules = policy.data["question_rules"]
    hits: dict[str, int] = {}
    for pattern in rules["identifier"]:
        for match in re.finditer(pattern, query):
            hits.setdefault(match.group(0), match.start())
    identifiers = tuple(sorted(hits, key=lambda h: (hits[h], h)))
    if question_type is not None:
        return question_type, identifiers
    if identifiers:
        return "exact", identifiers
    if any(re.search(p, query) for group in rules["current_state"].values() for p in group):
        return "current_state", identifiers
    return "thematic", identifiers


def route_for(policy: Policy, channel: str, kind: str, *, findings: bool) -> Route:
    route = policy.route(channel, kind)
    unbuilt = UNBUILT if findings else UNBUILT | {"L4"}
    layers = tuple(x for x in route["layers"] if x not in unbuilt)
    skipped = tuple(x for x in route["layers"] if x in unbuilt)
    return Route(f"{channel}.{kind}", layers, skipped, dict(route["filters"]), bool(route["rrf"]),
                 bool(route.get("l5_boost", False)))


def request_hash(**fields: Any) -> str:
    return hashlib.sha256(canonical(fields)).hexdigest()


def separated(policy: Policy, scores: Sequence[float]) -> bool:
    """FR-SEARCH-24 (4): the gate."""
    gate = policy.data["gate"]
    if gate["mode"] == "never_judge":
        return True
    if gate["mode"] == "always_judge":
        return False
    ordered = sorted(scores, reverse=True)
    return len(ordered) <= 1 or ordered[0] - ordered[1] >= gate["margin"]


def _by_score(cards: Sequence[Card]) -> list[Card]:
    return sorted(cards, key=lambda c: (-c.score, c.id))


def merge(cards: Sequence[Card], plus: Callable[[str, str], bool], seed: str) -> list[Card]:
    """FR-SEARCH-24 (3): pivot clusters; the pivot's text, every member's citations, the maximum score."""
    by_id = {c.id: c for c in cards}
    merged: list[Card] = []
    for cluster in greedy.pivot(list(by_id), plus, seed):
        head = by_id[cluster[0]]
        cites = list(head.cite)
        for member in cluster[1:]:
            cites.extend(x for x in by_id[member].cite if x.spn not in {y.spn for y in cites})
        merged.append(replace(head, cite=tuple(cites), score=max(by_id[m].score for m in cluster)))
    return _by_score(merged)


def expand(merged: Sequence[Card], neighbours: Mapping[str, Sequence[Card]], policy: Policy,
           layers: Sequence[str]) -> list[Card]:
    """FR-SEARCH-24 (5): one hop, `expand_per_card` per card at most, decayed scores, capped by score."""
    data = policy.data
    found: dict[str, Card] = {c.id: c for c in merged}
    for parent in merged:
        added = 0
        for target in neighbours.get(parent.id, ()):
            if added >= data["expand_per_card"]:
                break
            if target.layer not in layers:
                continue
            added += 1
            score = parent.score * data["expand_decay"]
            known = found.get(target.id)
            if known is None or score > known.score:
                found[target.id] = replace(known or target, score=score)
    return _by_score(list(found.values()))[: data["expand_cap"]]


def judge_slice(query: str, cards: Sequence[tuple[str, str]], limit: float) -> list[str]:
    """FR-SEARCH-28 (1): the longest prefix whose rendered judge input estimates ≤ limit."""
    taken: list[tuple[str, str]] = []
    for card in cards:
        if estimate(render_find_input(query, [*taken, card])) > limit:
            break
        taken.append(card)
    return [card_id for card_id, _ in taken]


def step7(
    f: greedy.Coverage, cost: Mapping[str, float], score: Mapping[str, float], budget: float,
    k: int | None, *, pool: Sequence[str], preselection: Sequence[str], judged_slice: Sequence[str],
    kept: Sequence[str] | None,
) -> list[tuple[str, bool]]:
    """FR-SEARCH-29: one pass without an applied output; else kept cards first, then the remainder."""
    def order(ids: Sequence[str]) -> list[str]:
        return sorted(ids, key=lambda c: (-score[c], c))

    if kept is None:
        return [(c, False) for c in order(greedy.budgeted(pool, f.value, cost, budget, score, k=k))]
    first = greedy.budgeted(kept, f.value, cost, budget, score, order=kept, k=k)
    chosen = set(first)
    pass1 = [c for c in kept if c in chosen]
    left = budget - sum(cost[c] for c in pass1)
    rest_k = None if k is None else k - len(pass1)
    remainder = [c for c in preselection if c not in set(judged_slice)]
    pass2: list[str] = []
    if remainder and rest_k != 0:
        gain = f.marginal(frozenset(pass1)).value
        pass2 = order(greedy.budgeted(remainder, gain, cost, left, score, k=rest_k))
    return [(c, True) for c in pass1] + [(c, False) for c in pass2]


def _served(card: Card, policy: Policy, channel: str) -> Card:
    if card.layer == "L1":
        text, more = card_shape.l1_text(card.text, policy, channel, entitled=card.entitled,
                                        shareable=card.source.license == card_shape.SHAREABLE)
        return replace(card, text=text, more=more)
    if card.layer == "L0":
        return replace(card, text=card_shape.cut(card.text, policy.data["card_budgets"]["L0"]))
    return card


def _envelope(brain_version: str, policy_version: str, budget_max: int) -> dict[str, Any]:
    return {
        "brain_version": brain_version, "policy_version": policy_version,
        "budget": int("9" * len(str(budget_max))),
        "cards": [], "judge_ran": False, "judge_source": _LONGEST_SOURCE, "judge_skip": _LONGEST_SKIP,
        "degraded": False,
    }


def _objective(policy: Policy, lexemes: frozenset[str], identifiers: Sequence[str], pool: Sequence[Card],
               s_top: float) -> greedy.Coverage:
    weights = policy.data["objective"]
    elements = greedy.elements(lexemes, identifiers, identifier_weight=weights["identifier_weight"],
                               lexeme_weight=weights["lexeme_weight"])
    lowered = [i.lower() for i in identifiers]
    covers = {
        c.id: set(c.covers) | {f"i:{i}" for i in lowered if i in c.text.lower()} for c in pool
    }
    return greedy.objective(elements, covers, {c.id: c.score for c in pool}, s_top=s_top,
                            relevance_weight=weights["relevance_weight"])


async def find(
    caller: Caller,
    channel: str,
    query: str,
    *,
    deps: Deps,
    filters: Mapping[str, Any] | None = None,
    k: int | None = None,
    budget: int | None = None,
    judge_ctx: Mapping[str, Any] | None = None,
    question_type: str | None = None,
) -> dict[str, Any]:
    policy = deps.policy or policy_mod.policy()
    request = validate(policy, channel, query, filters, k, budget, question_type)
    kind, identifiers = classify(policy, request.query, question_type, channel)
    route = route_for(policy, channel, kind, findings=deps.findings is not None)
    found = await deps.corpus.candidates(caller, channel, route, request.query, request.filters, identifiers)
    eligible = [c for c in found.cards if card_shape.eligible(c, channel)]
    pool = _by_score(eligible)[: policy.data["candidates_max"]]  # B6 step 2: at most candidates_max
    if "L4" in route.layers and deps.findings is not None:
        pool += [x.card for x in await deps.findings.for_query(caller, channel, request.query) if x.reviewed]
    pool = [_served(c, policy, channel) for c in pool]
    version = (deps.policy_version or (lambda: policy_mod.policy_version(policy)))()
    digest = request_hash(
        user_id=str(caller.user_id), roles=sorted(caller.roles), channel=channel, query=request.query,
        filters=request.filters, k=request.k, budget=request.budget, brain_version=found.brain_version,
        policy_version=version,
    )
    envelope = _envelope(found.brain_version, version, policy.channel(channel)["budget_max"])
    cards_budget = request.budget - estimate(card_shape.text_of(envelope))
    merged = merge(pool, deps.links, digest)
    s_top = merged[0].score if merged else 0.0
    kept: Sequence[str] | None = None
    source: str | None = None
    skip: str | None = None
    preselection: list[str] = []
    judged_slice: list[str] = []
    if separated(policy, [c.score for c in merged]):
        considered, skip = merged, "gate"
    else:
        near = await deps.corpus.neighbours(caller, channel, merged)
        eligible_near = {
            key: [_served(c, policy, channel) for c in group if card_shape.eligible(c, channel)]
            for key, group in near.items()
        }
        considered = expand(merged, eligible_near, policy, route.layers)
    by_id = {c.id: c for c in considered}
    cost = {c.id: card_shape.cost(c) for c in considered}
    score = {c.id: c.score for c in considered}
    f = _objective(policy, found.lexemes, identifiers, considered, s_top)
    if skip is None:
        factor = policy.data["preselect_factor"]
        chosen = greedy.budgeted(list(by_id), f.value, cost, factor * cards_budget, score)
        preselection = sorted(chosen, key=lambda c: (-score[c], c))
        l1 = policy.data["card_budgets"]["L1"]
        inputs = [(c, card_shape.cut(by_id[c].text, l1) if by_id[c].layer == "L1" else by_id[c].text)
                  for c in preselection]
        limit = min(factor * cards_budget, policy.data["judge"]["input_cap_tokens"])
        judged_slice = judge_slice(request.query, inputs, limit)
        if not judged_slice:
            skip = "slice_empty"
        elif deps.judge is None:
            skip = "error"
        else:
            sliced = set(judged_slice)
            outcome = await deps.judge(JudgeCall(
                caller=caller, channel=channel, query=request.query,
                cards=tuple(x for x in inputs if x[0] in sliced), request_hash=digest, judge_ctx=judge_ctx,
                pack_ids=tuple(sorted({by_id[c].pack_id for c in judged_slice})), cards_budget=cards_budget,
                preselection_cost=sum(cost[c] for c in preselection),
            ))
            if outcome.kept is None or outcome.source not in SOURCES:
                skip = outcome.skip if outcome.kept is None and outcome.skip in SKIPS else "error"
            else:
                kept = list(dict.fromkeys(c for c in outcome.kept if c in sliced))
                source = outcome.source
    picked = step7(f, cost, score, cards_budget, request.k, pool=list(by_id), preselection=preselection,
                   judged_slice=judged_slice, kept=kept)
    response: dict[str, Any] = {
        "brain_version": found.brain_version,
        "policy_version": version,
        "budget": request.budget,
        "cards": [card_shape.to_json(by_id[c], judged) for c, judged in picked],
        "judge_ran": kept is not None,
        "degraded": found.degraded or skip in DEGRADING,
    }
    if kept is not None:
        response["judge_source"] = source
    else:
        response["judge_skip"] = skip
    return response
