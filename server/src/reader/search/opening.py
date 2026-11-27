"""SRV-SEARCH FR-SEARCH-30, -31: an active pack's cards, the entitlement rule, the quote allowance and `open`.

A `Book` is one active pack read once (spans, outline, profile) and pure from there. Card ids: an L1 card is
the span's stored locator, an L2 card the outline id `sec:<sha>:<path>`, the L0 card `src:<sha>`. Reads go
through `reader.ingest.views`, the manifest and the profile read only (SRV-INGEST INV-INGEST-16).
"""

from __future__ import annotations

import re
import uuid
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import accumulate
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from reader.ingest import locator, manifest, packs, views
from reader.search import cards as card_shape
from reader.search import policy as policy_mod
from reader.search.cards import Card, Cite, Source
from reader.search.find import Caller, FindError, effective_budget
from reader.search.policy import CHANNELS, Policy, estimate

PERSONAL_USE: Final = "personal-use"  # a profile without a license fails closed (SRV-INGEST packs.shareable)
_SEC: Final = re.compile(r"sec:([0-9a-f]{64}):[1-9][0-9]{0,5}(?:/[1-9][0-9]{0,5}){0,15}")
_SRC: Final = re.compile(r"src:([0-9a-f]{64})")
_LATER: Final = ("con:", "fnd:", "les:")  # SRV-KB and SRV-LESSON provide them (declared)
_TITLE_SEP: Final = " / "


@dataclass(frozen=True)
class Span:
    locator: str
    page: int
    text: str
    quote_sha256: str


@dataclass(frozen=True)
class Node:
    id: str
    level: int
    title: str
    page: int | None


class Book:
    """One active pack's cards. A node's spans start at the first span on or after its page (never before the
    previous node's start); its direct spans run to the next node's start; its children are its direct spans,
    then its child nodes. Spans before the first node and the top nodes are the source card's children."""

    def __init__(self, material_sha256: str, pack_id: str, spans: Sequence[Span], nodes: Sequence[Node],
                 profile: Mapping[str, Any] | None, policy: Policy) -> None:
        self.sha, self.pack_id, self.policy = material_sha256, pack_id, policy
        self.spans, self.nodes = list(spans), list(nodes)
        values = profile or {}
        self.source = Source(id=f"src:{material_sha256}", role=values.get("role"), as_of=values.get("as_of"),
                             reliability=values.get("reliability"),
                             license=str(values.get("license") or PERSONAL_USE))
        self.language = values.get("language")
        n, m = len(self.spans), len(self.nodes)
        # linear in spans + nodes (code review of fb7b926): the first span on or after a page is a bisection
        # over the running maximum of span pages; parents and subtree ends come from one stack pass each
        reach = list(accumulate((s.page for s in self.spans), max))
        starts: list[int] = []
        for node in self.nodes:
            at = (starts[-1] if starts else 0) if node.page is None else bisect_left(reach, node.page)
            starts.append(max(at, starts[-1]) if starts else at)
        self.parent: list[int | None] = []
        stack: list[int] = []
        for i, node in enumerate(self.nodes):
            while stack and self.nodes[stack[-1]].level >= node.level:
                stack.pop()
            self.parent.append(stack[-1] if stack else None)
            stack.append(i)
        ends = [n] * m
        stack = []
        for i in range(m - 1, -1, -1):
            while stack and self.nodes[stack[-1]].level > self.nodes[i].level:
                stack.pop()
            ends[i] = starts[stack[-1]] if stack else n
            stack.append(i)
        following: list[int | None] = [None] * (n + 1)  # the first span at or after j with text
        for j in range(n - 1, -1, -1):
            following[j] = j if self.spans[j].text else following[j + 1]
        self.children: dict[str, list[str]] = {self.source.id: []}
        self.section: list[str] = [self.source.id] * n
        self.cited: list[int | None] = []
        self.paths: list[str] = []
        for i, node in enumerate(self.nodes):
            stop = starts[i + 1] if i + 1 < m else n
            self.children[node.id] = [self.spans[j].locator for j in range(starts[i], stop)]
            for j in range(starts[i], stop):
                self.section[j] = node.id
            hit = following[starts[i]]
            self.cited.append(hit if hit is not None and hit < ends[i] else None)
            up = self.parent[i]
            self.paths.append(node.title if up is None else self.paths[up] + _TITLE_SEP + node.title)
        first = starts[0] if starts else n
        self.children[self.source.id] = [s.locator for s in self.spans[:first]]
        for i, node in enumerate(self.nodes):
            up = self.parent[i]
            self.children[self.source.id if up is None else self.nodes[up].id].append(node.id)
        self.span_index = {s.locator: j for j, s in enumerate(self.spans)}
        self.node_index = {node.id: i for i, node in enumerate(self.nodes)}
        self.total = sum(len(s.text) for s in self.spans)
        limit = policy.data["quote_max_chars"]
        self.granted = card_shape.allowance(
            [(node.id, None if c is None else self.spans[c].locator,
              0 if c is None else len(card_shape.quote(self.spans[c].text, limit)), len(node.title))
             for node, c in zip(self.nodes, self.cited, strict=True)],
            self.total, policy.data["walk_quote_fraction_max"])
        # where L1 is not served, a node past the allowance is titled by its outline number ("1.2.3")
        self.closed_paths: list[str] = []
        for i, node in enumerate(self.nodes):
            own = node.title if node.id in self.granted else node.id.rsplit(":", 1)[1].replace("/", ".")
            up = self.parent[i]
            self.closed_paths.append(own if up is None else self.closed_paths[up] + _TITLE_SEP + own)

    @property
    def shareable(self) -> bool:
        return self.source.license == card_shape.SHAREABLE

    def l1_ok(self, channel: str, entitled: bool) -> bool:
        return card_shape.l1_allowed(channel, entitled=entitled, shareable=self.shareable)

    def visible(self, ids: Sequence[str], channel: str, entitled: bool) -> tuple[str, ...]:
        """FR-SEARCH-30: no link leads to an L1 id the channel may not serve this caller."""
        return tuple(i for i in ids if not i.startswith("spn:") or self.l1_ok(channel, entitled))

    def _card(self, card_id: str, layer: str, title: str, body: str, cite: tuple[Cite, ...],
              nxt: Sequence[str], channel: str, entitled: bool, more: bool = False) -> Card:
        return Card(id=card_id, layer=layer, title=title, text=body, cite=cite, source=self.source,
                    next=self.visible(nxt, channel, entitled), more=more, entitled=entitled,
                    pack_id=self.pack_id)

    def span_card(self, j: int, channel: str, entitled: bool) -> Card:
        span = self.spans[j]
        served, more = card_shape.l1_text(span.text, self.policy, channel, entitled=entitled,
                                          shareable=self.shareable)
        after = [self.spans[j + 1].locator] if j + 1 < len(self.spans) else []
        sec = self.section[j]
        title = self.paths[self.node_index[sec]] if sec in self.node_index else ""
        return self._card(span.locator, "L1", title, served, (Cite(span.locator, span.quote_sha256),),
                          [*after, sec], channel, entitled, more)

    def node_card(self, i: int, channel: str, entitled: bool) -> Card:
        node = self.nodes[i]
        tokens = card_shape.l2_budget(self.policy, has_children=any(p == i for p in self.parent),
                                      top_level=self.parent[i] is None)
        c = self.cited[i]
        full = None
        if c is not None and node.id in self.granted:
            full = card_shape.quote(self.spans[c].text, self.policy.data["quote_max_chars"])
        path = self.paths[i] if self.l1_ok(channel, entitled) else self.closed_paths[i]
        title, served = card_shape.l2_text(path, None, full, tokens)
        cite = () if c is None else (Cite(self.spans[c].locator, self.spans[c].quote_sha256, served),)
        return self._card(node.id, "L2", title, title, cite, self.children[node.id], channel, entitled)

    def source_card(self, channel: str, entitled: bool) -> Card:
        facts = {"license": self.source.license, "language": self.language, "role": self.source.role,
                 "reliability": self.source.reliability, "as_of": self.source.as_of}
        body = "; ".join(f"{k}: {v}" for k, v in facts.items() if v is not None)
        served = card_shape.cut(body, self.policy.data["card_budgets"]["L0"])
        return self._card(self.source.id, "L0", "", served, (), self.children[self.source.id], channel,
                          entitled)

    def card(self, card_id: str, channel: str, entitled: bool) -> Card | None:
        """The card of an id the channel may serve this caller; None for an unknown or ineligible id."""
        if card_id == self.source.id:
            return self.source_card(channel, entitled)
        if card_id in self.node_index:
            return self.node_card(self.node_index[card_id], channel, entitled)
        j = self.span_index.get(card_id)
        if j is None or not self.l1_ok(channel, entitled):
            return None
        return self.span_card(j, channel, entitled)

    def descendants(self, card_id: str, depth: int, channel: str, entitled: bool) -> list[Card]:
        """Children down to `depth` in pre-order, skipping ineligible L1 children (L1 cards have none)."""
        out: list[Card] = []
        if depth <= 0 or card_id not in self.children:
            return out
        for child in self.visible(self.children[card_id], channel, entitled):
            found = self.card(child, channel, entitled)
            if found is not None:
                out.append(found)
                out.extend(self.descendants(child, depth - 1, channel, entitled))
        return out


async def load_book(conn: AsyncConnection, pack: views.Pack, policy: Policy) -> Book:
    spans = [Span(s.locator, s.page, s.text, s.quote_sha256 or locator.quote_sha256(s.text))
             for s in await views.pack_spans(conn, pack) if s.locator is not None]
    outline = await views.pack_outline(conn, pack)
    paths = locator.outline_paths([o.level for o in outline])
    nodes = [Node(locator.outline_id(pack.material_sha256, p), o.level, o.title, o.page)
             for o, p in zip(outline, paths, strict=True)]
    profile = await packs.profile_for_pack(conn, pack.owner_id, pack.pack_id)
    return Book(pack.material_sha256, pack.pack_id, spans, nodes, profile, policy)


async def entitled(conn: AsyncConnection, user_id: uuid.UUID, pack: views.Pack) -> bool:
    """FR-SEARCH-30: the caller owns the pack, or holds a live material row of the same sha256 (computed by
    our code from that caller's bytes at receive); an uploads row or a client-given sha256 never counts."""
    if pack.owner_id == user_id:
        return True
    row = await conn.execute(text(
        "SELECT 1 FROM materials WHERE owner_id = :u AND sha256 = :s AND deleted_at IS NULL"),
        {"u": user_id, "s": pack.material_sha256})
    return row.first() is not None


async def quote_allowance(conn: AsyncConnection, owner_id: uuid.UUID, material_sha256: str,
                          policy: Policy | None = None) -> frozenset[str] | None:
    """The granted node ids over the material's active pack; None when it has no active pack."""
    pack = await views.active_pack(conn, owner_id, material_sha256)
    if pack is None:
        return None
    return (await load_book(conn, pack, policy or policy_mod.policy())).granted


def material_of(card_id: str) -> str:
    """The material sha256 an id names; `id_invalid` when malformed, `not_found` for kinds not built yet."""
    if card_id.startswith(_LATER):
        raise FindError("not_found")
    if card_id.startswith("spn:"):
        try:
            return locator.parse(card_id).material_sha256
        except locator.LocatorError:
            raise FindError("id_invalid") from None
    for pattern in (_SEC, _SRC):
        match = pattern.fullmatch(card_id)
        if match is not None:
            return match.group(1)
    raise FindError("id_invalid")


def _fits(response: Mapping[str, Any], budget: int) -> bool:
    return estimate(card_shape.text_of(response)) <= budget


def _widened(book: Book, card: Card, response: dict[str, Any], budget: int) -> Card:
    """A shareable span opened directly: its full text within what the response leaves (`more` when cut)."""
    full = book.spans[book.span_index[card.id]].text
    trial = dict(response, cards=[card_shape.to_json(replace(card, text="", more=True), False)])
    left = budget - estimate(card_shape.text_of(trial))
    while left > 0:
        body = card_shape.cut(full, left)
        widened = replace(card, text=body, more=body != full)
        body_json = card_shape.to_json(widened, False)
        over = estimate(card_shape.text_of(dict(response, cards=[body_json]))) - budget
        if over <= 0:
            return widened
        left -= over
    return replace(card, text="", more=bool(full))


async def open_(
    conn: AsyncConnection, caller: Caller, channel: str, card_id: str, *, depth: int = 1,
    budget: int | None = None, policy: Policy | None = None, policy_version: str | None = None,
) -> dict[str, Any]:
    """FR-SEARCH-31: the card of a typed id and its descendants to `depth`, in order, within the budget."""
    loaded = policy or policy_mod.policy()
    if channel not in CHANNELS:
        raise FindError("channel_invalid")
    deepest = loaded.data["open_depth_max"]
    if isinstance(depth, bool) or not isinstance(depth, int) or not 1 <= depth <= deepest:
        raise FindError("depth_invalid")
    effective = effective_budget(loaded, channel, budget)
    if not isinstance(card_id, str):
        raise FindError("id_invalid")
    sha = material_of(card_id)
    pack = await views.active_pack(conn, caller.user_id, sha)
    if pack is None:
        raise FindError("not_found")
    book = await load_book(conn, pack, loaded)
    allowed = await entitled(conn, caller.user_id, pack)
    head = book.card(card_id, channel, allowed)
    if head is None:
        raise FindError("not_found")
    view = await manifest.active_view(conn, caller.user_id)
    response: dict[str, Any] = {
        "brain_version": view.brain_version,
        "policy_version": policy_version or policy_mod.policy_version(loaded),
        "budget": effective,
        "cards": [],
    }
    if head.layer == "L1" and book.shareable:
        head = _widened(book, head, response, effective)
    for card in [head, *book.descendants(card_id, depth, channel, allowed)]:
        body = card_shape.to_json(card, False)
        if _fits(dict(response, cards=[*response["cards"], body]), effective):
            response["cards"].append(body)
    return response
