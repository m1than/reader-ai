"""The text-free `find` spans (SRV-OBS FR-OBS-36; ADR-0039 B9, ADR-0032 Decision 5 as amended).

SRV-SEARCH opens one `find` subtree per call through these helpers: `find` (TOOL) under the current span
(a request's SERVER span or a job root) or as a new trace, with `find.route` (ROUTER), `find.retrieve`
(RETRIEVER, one per layer), `find.preselect` (RERANKER), `find.judge_wait` (CHAIN, only when the judge is
attempted) and `find.select` (RERANKER) as its children. Each opener and each `done` takes ids, enums,
counts and hashes only, never a string from a query, a card or a filter value: card ids become document
uris through `doc_uri`, question identifiers a sha256 through `identifiers_sha256`, a concept id
`reader://h/<sha256>` through `concept_uri`. Parameters map to VEC-OBS-1 keys through `KEYS`; the exporter
validates every value like any other. None of the helpers raises, and with tracing off none of them does
anything (they wrap `reader.obs.spans.child_span`).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final, Literal, NewType

from reader.obs.attributes import CLEAR_ID, DOC_CAP, doc_key
from reader.obs.spans import Child, Hex64, child_span, keyed

Uri = NewType("Uri", str)  # reader://<clear id> or reader://h/<64 hex>
CardId = NewType("CardId", str)  # spn:, fnd:, les:, sec:, src: or con: id; in clear only when doc_uri allows
ConceptId = NewType("ConceptId", str)
RouteId = NewType("RouteId", str)  # a route id of SRV-SEARCH's declared route table
FacetValue = NewType("FacetValue", str)  # a value of the facets file
TopicPath = NewType("TopicPath", str)  # a reader.yaml `sectors` path
ModelId = NewType("ModelId", str)  # a pricing table model id

Channel = Literal["mcp", "tutor", "pipeline", "owner"]
QuestionType = Literal["exact", "thematic", "current_state", "fact_check"]
JudgeSource = Literal["live", "recorded"]
JudgeSkip = Literal[
    "gate", "slice_empty", "timeout", "error", "budget", "saturated", "quota_refused", "no_provider"
]
Layer = Literal["L1", "L2", "L3", "L4"]
Method = Literal["dense", "dense_fts_rrf", "outline", "registry"]
Gate = Literal["separated", "not_separated"]
Reliability = Literal["A", "B", "C", "D", "E", "F"]

KEYS: Final[Mapping[str, str]] = {
    "channel": "reader.find.channel",
    "caller_model": "reader.caller.model",
    "brain_version": "reader.brain_version",
    "policy_version": "reader.policy_version",
    "question_type": "reader.find.question_type",
    "entitled": "reader.find.entitled",
    "k": "reader.find.k",
    "budget_tokens": "reader.find.budget_tokens",
    "cards": "reader.find.cards",
    "tokens": "reader.find.tokens",
    "judged_tokens": "reader.find.judged_tokens",
    "unjudged_tokens": "reader.find.unjudged_tokens",
    "route": "reader.find.route",
    "role": "reader.find.filter.role",
    "form": "reader.find.filter.form",
    "reliability_min": "reader.find.filter.reliability_min",
    "topic": "reader.find.filter.topic",
    "identifiers": "reader.find.filter.identifiers",
    "identifiers_sha256": "reader.find.filter.identifiers_sha256",
    "concept_uri": "reader.find.filter.concept_uri",
    "layer": "reader.layer",
    "method": "reader.find.method",
    "candidates": "reader.retrieval.candidates",
    "hits": "reader.retrieval.hits",
    "merged": "reader.find.merged",
    "gate": "reader.find.gate",
    "expanded": "reader.find.expanded",
    "candidates_in": "reader.find.candidates_in",
    "input_sha256": "reader.judge.input_sha256",
    "input_tokens": "reader.judge.input_tokens",
    "kept": "reader.judge.kept",
    "dropped": "reader.judge.dropped",
}
_CLEAR: Final = re.compile(CLEAR_ID)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def doc_uri(card_id: CardId) -> Uri:
    """`reader://<id>` for a PDF span locator, a finding or a lesson; `reader://h/<sha256 of the id>` for
    every other id, which may carry heading, file-path or term text of the material."""
    if _CLEAR.fullmatch(card_id):
        return Uri(f"reader://{card_id}")
    return Uri(f"reader://h/{_sha(card_id)}")


def concept_uri(concept_id: ConceptId) -> Uri:
    """A registry concept, never in clear: a term concept's id may carry book text."""
    return Uri(f"reader://h/{_sha(concept_id)}")


def _normalised(identifier: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", identifier).casefold().split())


def identifiers_sha256(identifiers: Iterable[str]) -> Hex64:
    """sha256 of the sorted, normalised identifiers (NFKC, casefolded, whitespace runs collapsed, trimmed)
    joined by U+001F. Takes the extracted strings and returns only their hash."""
    return Hex64(_sha("\x1f".join(sorted(_normalised(i) for i in identifiers))))


@dataclass(frozen=True)
class Doc:
    """One returned card as a document: its uri (from `doc_uri`), content sha256, tokens and, on find.select
    only, whether the judge saw it."""

    uri: Uri
    sha256: Hex64
    tokens: int
    judged: bool | None = None


def doc(card_id: CardId, sha256: Hex64, tokens: int, judged: bool | None = None) -> Doc:
    return Doc(doc_uri(card_id), sha256, tokens, judged)


def documents(docs: Sequence[Doc]) -> dict[str, Any]:
    """The first DOC_CAP documents as reader.doc.<i>.* keys in the given order; the rest are only counted."""
    out: dict[str, Any] = {}
    for i, d in enumerate(docs[:DOC_CAP]):
        out[doc_key(i, "uri")] = d.uri
        out[doc_key(i, "sha256")] = d.sha256
        out[doc_key(i, "tokens")] = d.tokens
        if d.judged is not None:
            out[doc_key(i, "judged")] = d.judged
    return out


@dataclass(frozen=True)
class Judge:
    """The judge's outcome: `source` is kept only when it ran, `skip` only when it did not."""

    ran: bool
    source: JudgeSource | None = None
    skip: JudgeSkip | None = None

    def attributes(self) -> dict[str, Any]:
        extra = {"reader.judge_source": self.source} if self.ran else {"reader.judge_skip": self.skip}
        return {"reader.judge_ran": self.ran, **{k: v for k, v in extra.items() if v is not None}}


class _Part:
    def __init__(self, child: Child) -> None:
        self._child = child

    def _set(self, values: Mapping[str, Any]) -> None:
        attributes = keyed(KEYS, values)
        if (docs := values.get("docs")) is not None:
            attributes.update(documents(docs))
        if (judge := values.get("judge")) is not None:
            attributes.update(judge.attributes())
        self._child.set(attributes)


class FindPart(_Part):
    def done(
        self, *, cards: int, tokens: int, judged_tokens: int, unjudged_tokens: int, judge: Judge,
    ) -> None:
        self._set(locals())


class RoutePart(_Part):
    def done(
        self, *, route: RouteId, role: FacetValue | None = None, form: FacetValue | None = None,
        reliability_min: Reliability | None = None, topic: TopicPath | None = None, identifiers: int = 0,
        identifiers_sha256: Hex64 | None = None, concept_uri: Uri | None = None,
    ) -> None:
        self._set(locals())


class RetrievePart(_Part):
    def done(self, *, candidates: int, hits: int, docs: Sequence[Doc]) -> None:
        self._set(locals())


class PreselectPart(_Part):
    def done(
        self, *, merged: int, gate: Gate, expanded: bool, candidates_in: int, cards: int, tokens: int,
        budget_tokens: int, docs: Sequence[Doc],
    ) -> None:
        self._set(locals())


class JudgeWaitPart(_Part):
    def done(self, *, kept: int, dropped: int, judge: Judge) -> None:
        self._set(locals())


class SelectPart(_Part):
    def done(
        self, *, cards: int, tokens: int, judged_tokens: int, unjudged_tokens: int, budget_tokens: int,
        docs: Sequence[Doc],
    ) -> None:
        self._set(locals())


@contextmanager
def find(
    *, channel: Channel, question_type: QuestionType, entitled: bool, k: int, budget_tokens: int,
    brain_version: Hex64, policy_version: Hex64, caller_model: ModelId | None = None,
) -> Iterator[FindPart]:
    """(a) The subtree's TOOL span; `caller_model` is omitted when the caller's model is unknown."""
    with child_span("find", keyed(KEYS, locals())) as child:
        yield FindPart(child)


@contextmanager
def route() -> Iterator[RoutePart]:
    """(b) ROUTER: the route id and filters as enums, question-derived filters as counts and hashes."""
    with child_span("find.route") as child:
        yield RoutePart(child)


@contextmanager
def retrieve(*, layer: Layer, method: Method) -> Iterator[RetrievePart]:
    """(c) RETRIEVER, one per layer used."""
    with child_span("find.retrieve", keyed(KEYS, locals())) as child:
        yield RetrievePart(child)


@contextmanager
def preselect() -> Iterator[PreselectPart]:
    """(d) RERANKER."""
    with child_span("find.preselect") as child:
        yield PreselectPart(child)


@contextmanager
def judge_wait(*, input_sha256: Hex64, input_tokens: int) -> Iterator[JudgeWaitPart]:
    """(e) CHAIN, only when the judge is attempted. Enqueue `judge.find` while this span is current, so the
    job's own trace links to it (FR-OBS-5 b)."""
    with child_span("find.judge_wait", keyed(KEYS, locals())) as child:
        yield JudgeWaitPart(child)


@contextmanager
def select() -> Iterator[SelectPart]:
    """(f) RERANKER: the returned cards in response order."""
    with child_span("find.select") as child:
        yield SelectPart(child)
