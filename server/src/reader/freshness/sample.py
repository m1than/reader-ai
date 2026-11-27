"""The deterministic sample (FR-FRESH-2), the topics (FR-FRESH-4) and scan_inputs_sha256 (FR-FRESH-3).

Front spans: the spans of the first `front_pages` pages in `seq` order, each cut to `span_chars_max`, until
the next one would pass `front_chars_max`. Then the other spans of kind text, list item, table or code,
grouped into sections (level-1 outline entries with a page, each from its page to the next entry's; without
them, equal page ranges of ceil(page_count / 20)), ranked inside each section by the dated-claim score (ties
by `seq`), and taken round-robin by rank until the next one would pass `sample_chars_max`.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from reader.freshness import params
from reader.freshness.signals import score
from reader.freshness.text import cut, sanitise

BODY_KINDS: Final = frozenset({"text", "list_item", "table", "code"})
LOCATOR_PREFIX: Final = "spn:"  # SRV-INGEST FR-INGEST-33's locator form; a legacy id never starts with it
SECTIONS_WITHOUT_OUTLINE: Final = 20


@dataclass(frozen=True)
class SpanRow:
    seq: int
    page: int
    kind: str
    text: str
    span_id: str | None = None  # SRV-INGEST views' id (FR-FRESH-2's paired change); None: `<sha>:<seq>`
    quote_sha256: str | None = None  # the stored quote hash (SRV-INGEST FR-INGEST-34)


@dataclass(frozen=True)
class OutlineRow:
    seq: int
    level: int
    title: str
    page: int | None


@dataclass(frozen=True)
class Picked:
    seq: int
    page: int
    kind: str
    text: str  # cut to span_chars_max
    front: bool
    span_id: str | None = None
    quote_sha256: str | None = None

    def id_in(self, material_sha256: str) -> str:
        return self.span_id or span_id(material_sha256, self.seq)

    def ref(self, material_sha256: str) -> dict[str, str]:
        """The span id, plus its quote_sha256 beside a locator (SRV-INGEST FR-INGEST-34; r3_gates_revision).
        A locator without a stored hash cannot occur (an active pack is sealed): asserted, so the job fails
        closed instead of storing a locator without its hash."""
        sid = self.id_in(material_sha256)
        if not sid.startswith(LOCATOR_PREFIX):  # a legacy `<sha>:<seq>` id carries no hash
            return {"span": sid}
        assert self.quote_sha256 is not None, "locator span without its quote hash"  # noqa: S101
        return {"span": sid, "quote_sha256": self.quote_sha256}


@dataclass(frozen=True)
class Sample:
    spans: tuple[Picked, ...]  # in seq order

    @property
    def chars(self) -> int:
        return sum(len(p.text) for p in self.spans)

    @property
    def seqs(self) -> list[int]:
        return [p.seq for p in self.spans]

    def stored(self, material_sha256: str) -> list[dict[str, object]]:
        """What the report stores: span ids (with a locator's quote hash) and pages only (FR-FRESH-2)."""
        return [{**p.ref(material_sha256), "page": p.page} for p in self.spans]

    @property
    def non_front_texts(self) -> list[str]:
        return [p.text for p in self.spans if not p.front]


@dataclass(frozen=True)
class Topic:
    id: int  # the outline entry's seq: the section id recommendations reference
    title: str
    level: int


def span_id(material_sha256: str, seq: int) -> str:
    """SRV-INGEST FR-INGEST-17's public span id (a legacy pack's; an r3 pack's spans carry their locator)."""
    return f"{material_sha256}:{seq}"


def _sections(
    others: Sequence[SpanRow], outline: Sequence[OutlineRow], page_count: int
) -> list[list[SpanRow]]:
    starts = [o.page for o in sorted(outline, key=lambda o: o.seq) if o.level == 1 and o.page is not None]
    groups: dict[int, list[SpanRow]] = {}
    if starts:
        for span in others:
            index = 0
            for i, start in enumerate(starts):
                if start <= span.page:
                    index = i
            groups.setdefault(index, []).append(span)
    else:
        size = max(1, math.ceil(page_count / SECTIONS_WITHOUT_OUTLINE))
        for span in others:
            groups.setdefault((span.page - 1) // size, []).append(span)
    return [groups[i] for i in sorted(groups)]


def build(spans: Sequence[SpanRow], outline: Sequence[OutlineRow], page_count: int, lang: str) -> Sample:
    ordered = sorted(spans, key=lambda s: s.seq)
    picked: list[Picked] = []
    used = 0
    for span in ordered:
        if span.page > params.FRONT_PAGES:
            continue
        piece = cut(span.text, params.SPAN_CHARS_MAX)
        if not piece.strip():
            continue
        if used + len(piece) > params.FRONT_CHARS_MAX:
            break
        picked.append(Picked(span.seq, span.page, span.kind, piece, True, span.span_id, span.quote_sha256))
        used += len(piece)
    front = {p.seq for p in picked}
    others = [s for s in ordered if s.seq not in front and s.kind in BODY_KINDS and s.text.strip()]
    ranked = [
        sorted(group, key=lambda s: (-score(s.text, lang), s.seq))
        for group in _sections(others, outline, page_count)
    ]
    rank = 0
    full = False
    while not full and any(rank < len(group) for group in ranked):
        for group in ranked:
            if rank >= len(group):
                continue
            span = group[rank]
            piece = cut(span.text, params.SPAN_CHARS_MAX)
            if used + len(piece) > params.SAMPLE_CHARS_MAX:
                full = True
                break
            picked.append(
                Picked(span.seq, span.page, span.kind, piece, False, span.span_id, span.quote_sha256)
            )
            used += len(piece)
        rank += 1
    return Sample(tuple(sorted(picked, key=lambda p: p.seq)))


def topics(outline: Sequence[OutlineRow]) -> list[Topic]:
    """FR-FRESH-4: level 1 and 2 titles in outline order, sanitised and cut, the first `topics_max`."""
    found: list[Topic] = []
    for entry in sorted(outline, key=lambda o: o.seq):
        if entry.level > 2:
            continue
        title = sanitise(entry.title, params.TOPIC_CHARS_MAX)
        if title:
            found.append(Topic(entry.seq, title, entry.level))
        if len(found) == params.TOPICS_MAX:
            break
    return found


def canonical_sha256(value: object) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def scan_inputs_sha256(
    ingest_id: str, span_ids: Sequence[str], prompt_version: str, prompt_sha256: str, model: str
) -> str:
    """FR-FRESH-3: the scan's reuse key; it excludes the bibliography."""
    return canonical_sha256(
        {
            "ingest": ingest_id,
            "spans": list(span_ids),
            "prompt_version": prompt_version,
            "prompt_sha256": prompt_sha256,
            "model": model,
        }
    )
