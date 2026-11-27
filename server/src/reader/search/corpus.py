"""The production `Corpus` port of `find` (SRV-SEARCH FR-SEARCH-7, B6 step 2 and step 5's links).

Candidates are FR-SEARCH-7's span candidates over the caller's active packs as L1 cards with their full
span text (find serves the cut); a pack whose L1 cards the channel may not serve this caller is left out of
the scan (FR-SEARCH-30). Neighbours are an L1 card's two 1-hop links, its next span and its section, as cards
of the same pack's `Book`. INV-SEARCH-1: only the caller's own head entry is read.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from sqlalchemy.ext.asyncio import AsyncConnection

from reader.ingest import views
from reader.search import opening, store
from reader.search.cards import Card
from reader.search.find import Caller, Candidates, Route
from reader.search.policy import Policy


@dataclass
class StoreCorpus:
    conn: AsyncConnection
    policy: Policy
    _books: dict[str, opening.Book] = field(default_factory=dict)

    async def _book(self, pack: views.Pack) -> opening.Book:
        if pack.pack_id not in self._books:
            self._books[pack.pack_id] = await opening.load_book(self.conn, pack, self.policy)
        return self._books[pack.pack_id]

    async def _packs(self, caller: Caller) -> dict[str, views.Pack]:
        return {p.pack_id: p for p in (await views.active_packs(self.conn, caller.user_id)).values()}

    async def candidates(
        self, caller: Caller, channel: str, route: Route, query: str, filters: Mapping[str, object],
        identifiers: Sequence[str],
    ) -> Candidates:
        del identifiers
        active = await self._packs(caller)
        excluded = []
        for pack in active.values():
            book = await self._book(pack)
            if not book.l1_ok(channel, await opening.entitled(self.conn, caller.user_id, pack)):
                excluded.append(pack.pack_id)
        data = self.policy.data
        found = await store.candidates(
            self.conn, caller.user_id, query, limit=data["candidates_max"], use_rrf=route.rrf,
            rrf_k=data["rrf_k"], w_dense=float(data["w_dense"]), exclude_packs=excluded,
            filters={k: str(v) for k, v in filters.items()},
        )
        cards: list[Card] = []
        for hit in found.hits if "L1" in route.layers else []:
            book = await self._book(active[hit.pack_id])
            j = book.span_index[hit.locator]
            card = replace(book.span_card(j, channel, True), text=book.spans[j].text, more=False)
            cards.append(replace(card, score=hit.score, covers=hit.covers))
        return Candidates(cards, found.degraded, found.brain_version, found.lexemes, frozenset(route.layers))

    async def neighbours(
        self, caller: Caller, channel: str, cards: Sequence[Card]
    ) -> Mapping[str, Sequence[Card]]:
        active = await self._packs(caller)
        out: dict[str, list[Card]] = {}
        for card in cards:
            pack = active.get(card.pack_id)
            if pack is None or card.layer != "L1":
                continue
            book = await self._book(pack)
            near: list[Card] = []
            for target in card.next:
                found = book.card(target, channel, True)
                if found is None:
                    continue
                if found.layer == "L1":
                    found = replace(found, text=book.spans[book.span_index[target]].text, more=False)
                near.append(found)
            out[card.id] = near
        return out
