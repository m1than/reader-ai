"""SRV-SEARCH FR-SEARCH-22, -30: the brain/v1 card shape, its cost, text cuts and channel eligibility."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from reader.agent.ledger import CHARS_PER_TOKEN_FLOOR
from reader.search.policy import Policy, estimate

SHAREABLE: Final = "shareable"
SEPARATOR_TOKENS: Final = 0.5  # one character's estimate: the `,` between two cards (FR-SEARCH-22)


@dataclass(frozen=True)
class Cite:
    spn: str
    quote_sha256: str
    quote: str | None = None

    def to_json(self) -> dict[str, Any]:
        body: dict[str, Any] = {"spn": self.spn, "quote_sha256": self.quote_sha256}
        if self.quote is not None:
            body["quote"] = self.quote
        return body


@dataclass(frozen=True)
class Source:
    id: str
    role: str | None
    as_of: str | None
    reliability: str | None
    license: str


@dataclass(frozen=True)
class Card:
    id: str
    layer: str
    title: str
    text: str
    cite: tuple[Cite, ...]
    source: Source
    next: tuple[str, ...] = ()
    score: float = 0.0
    covers: frozenset[str] = frozenset()  # FR-SEARCH-25 element keys (l:<lexeme>, i:<identifier>)
    more: bool = False
    entitled: bool = True  # FR-SEARCH-30: the caller owns the pack or holds an own copy of its sha256
    pack_id: str = ""


def to_json(card: Card, judged: bool) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": card.id,
        "layer": card.layer,
        "title": card.title,
        "text": card.text,
        "cite": [c.to_json() for c in card.cite],
        "source": {
            "id": card.source.id, "role": card.source.role, "as_of": card.source.as_of,
            "reliability": card.source.reliability, "license": card.source.license,
        },
        "next": list(card.next),
        "judged": judged,
    }
    if card.more:
        body["more"] = True
    return body


def text_of(value: Any) -> str:
    """Canonical JSON as text (the estimator counts code points, not bytes)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def cost(card: Card) -> float:
    """A card's cost in the greedy: the estimate of its whole JSON (`judged` false, the longer literal) plus
    its separator."""
    return estimate(text_of(to_json(card, judged=False))) + SEPARATOR_TOKENS


def chars(tokens: float) -> int:
    return math.floor(tokens * float(CHARS_PER_TOKEN_FLOOR))


def cut_chars(text: str, limit: int) -> str:
    """At the last whitespace at or before the limit, hard when there is none."""
    if len(text) <= limit:
        return text
    head = text[: limit + 1]
    for i in range(len(head) - 1, 0, -1):  # whitespace only at 0 would leave nothing: cut hard instead
        if head[i].isspace():
            return text[:i]
    return text[:limit]


def cut(text: str, tokens: float) -> str:
    return cut_chars(text, chars(tokens))


def l1_text(text: str, policy: Policy, channel: str, *, entitled: bool, shareable: bool) -> tuple[str, bool]:
    """L1 text cut at its card budget; not cut on the pipeline for an entitled caller; `more` only when a
    shareable card was cut."""
    if channel == "pipeline" and entitled:
        return text, False
    served = cut(text, policy.data["card_budgets"]["L1"])
    return served, shareable and served != text


def l2_budget(policy: Policy, *, has_children: bool, top_level: bool) -> int:
    budgets: Mapping[str, int] = policy.data["card_budgets"]["L2"]
    if not has_children:
        return budgets["leaf"]
    return budgets["root"] if top_level else budgets["chapter"]


def l2_text(title: str, summary: str | None, quote: str | None, tokens: int) -> tuple[str, str | None]:
    """The title path first (alone over the budget: cut, no quote), then the summary, then the quote takes
    what is left of the same budget, cut at whitespace, omitted when nothing is left (VEC-SEARCH-9)."""
    limit = chars(tokens)
    if len(title) > limit:
        return cut_chars(title, limit), None
    text = title if not summary else cut_chars(title + "\n" + summary, limit)
    left = limit - len(text)
    if quote is None or left <= 0:
        return text, None
    served = cut_chars(quote, left)
    return text, served or None


def l1_allowed(channel: str, *, entitled: bool, shareable: bool) -> bool:
    """FR-SEARCH-30: L1 of a personal-use pack only for an entitled caller and never on mcp."""
    return shareable or (channel != "mcp" and entitled)


def eligible(card: Card, channel: str) -> bool:
    """FR-SEARCH-30: a personal-use L1 card is served only to an entitled caller and never on mcp."""
    if card.layer != "L1":
        return True
    return l1_allowed(channel, entitled=card.entitled, shareable=card.source.license == SHAREABLE)


def quote(text: str, max_chars: int) -> str:
    """FR-SEARCH-30: the first ≤ max_chars code points of a span's text, cut at whitespace (hard without)."""
    return cut_chars(text, max_chars)


def allowance(nodes: Sequence[tuple[str, str | None, int]], total: int, fraction: float) -> frozenset[str]:
    """FR-SEARCH-30's quote allowance over (node id, cited span or None, full quote length) in outline order:
    each distinct cited span counts once; the walk stops at the first node that would pass fraction * total,
    so the granted set is a prefix of the order."""
    limit = fraction * total
    counted: set[str] = set()
    used = 0
    granted: list[str] = []
    for node_id, span, length in nodes:
        extra = 0 if span is None or span in counted else length
        if used + extra > limit:
            break
        used += extra
        if span is not None:
            counted.add(span)
        granted.append(node_id)
    return frozenset(granted)
