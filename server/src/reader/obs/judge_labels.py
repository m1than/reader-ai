"""SRV-OBS FR-OBS-40 (ADR-0039 B7 labels items 2-4): the owner's judge labelling reads.

An item is a `judge_inputs` row (slot judge.find, id `ji:<uuid>`) or an `obs_eval_scores` row (its slot, id
`es:<uuid>`). The queue orders unlabelled items by B7 item 3's classes, then round-robin over (route, first pack)
groups, newest first. An item's card text comes from the card-reader port in memory, as the owner may read it
under B8 (entitled text, or the MCP row with no L1 text); nothing here stores or logs it.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, Protocol

from sqlalchemy import text

ITEM: Final = re.compile(r"(ji|es):([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
QUEUE_MAX: Final = 25  # initial value (B7 item 4: batches of 25)
FETCH_MAX: Final = 1000  # newest unlabelled rows read per queue request (structural bound)
DISAGREE_BELOW: Final = 0.5  # initial value: a relevance score under it disagrees with a judge.find keep
NEAR: Final = 0.1  # initial value: "within ±0.1 of the slot's threshold"
FIND: Final = "judge.find"


class NotLabelable(Exception):
    """409 `not_labelable`: a row without consent, or cards that no longer resolve."""


@dataclass(frozen=True)
class CardView:
    id: str
    text: str | None
    view: Literal["entitled", "mcp", "unavailable"]


class CardReader(Protocol):
    """SRV-SEARCH's card-text-by-id reader with B8 entitlement (paired V.kl-search-find)."""

    async def read(self, conn: Any, viewer: uuid.UUID, card_ids: list[str]) -> list[CardView]: ...

    async def resolvable(self, conn: Any, card_ids: list[str]) -> bool: ...


class Unwired:
    """The default until SRV-SEARCH's reader exists on this branch: ids only, no text."""

    async def read(self, conn: Any, viewer: uuid.UUID, card_ids: list[str]) -> list[CardView]:
        return [CardView(id=c, text=None, view="unavailable") for c in card_ids]

    async def resolvable(self, conn: Any, card_ids: list[str]) -> bool:
        return True


CARDS: CardReader = Unwired()
# The policy's thresholds by slot (SRV-SEARCH: judge.find.sufficiency_threshold, judge.eval.<name>.pass_threshold).
THRESHOLDS: Callable[[], Mapping[str, float]] = dict


@dataclass(frozen=True)
class Item:
    id: str
    slot: str
    created_at: datetime
    route: str | None
    channel: str | None
    group: tuple[str, str]
    klass: int


def parse_item(raw: str) -> tuple[str, uuid.UUID] | None:
    found = ITEM.fullmatch(raw) if isinstance(raw, str) else None
    return (found.group(1), uuid.UUID(found.group(2))) if found else None


def order(items: Sequence[Item], limit: int) -> list[Item]:
    """By class; within a class round-robin over groups (the group with the newest item first), newest first."""
    out: list[Item] = []
    for klass in sorted({i.klass for i in items}):
        groups: dict[tuple[str, str], list[Item]] = {}
        for item in sorted((i for i in items if i.klass == klass), key=lambda i: i.created_at, reverse=True):
            groups.setdefault(item.group, []).append(item)
        lanes = list(groups.values())
        while any(lanes):
            for lane in lanes:
                if lane:
                    out.append(lane.pop(0))
    return out[:limit]


_UNLABELLED: Final = (
    " AND NOT EXISTS (SELECT 1 FROM owner_labels l WHERE l.owner_id = :o AND l.target = 'judge'"
    " AND l.target_id = {col})"
)
INPUTS_SQL: Final = (
    "SELECT ji.id, ji.created_at, ji.route, ji.channel, ji.pack_ids, ji.output, ji.find_trace_id,"
    " EXISTS (SELECT 1 FROM obs_eval_scores s WHERE s.judge_input_id = ji.id AND s.slot = 'judge.eval.relevance'"
    " AND s.value < :below) AS disagree FROM judge_inputs ji WHERE ji.labelable"
    + _UNLABELLED.format(col="ji.id")
    + " ORDER BY ji.created_at DESC LIMIT :n"
)
SCORES_SQL: Final = (
    "SELECT s.id, s.created_at, s.value, s.judge_input_id, ji.route, ji.channel, ji.pack_ids,"
    " (s.slot = 'judge.eval.relevance' AND s.value < :below AND ji.id IS NOT NULL"
    " AND jsonb_array_length(ji.output->'keep') > 0) AS disagree"
    " FROM obs_eval_scores s LEFT JOIN judge_inputs ji ON ji.id = s.judge_input_id"
    " WHERE s.owner_id = :o AND s.slot = :slot AND (s.judge_input_id IS NULL OR ji.labelable)"
    + _UNLABELLED.format(col="s.id")
    + " ORDER BY s.created_at DESC LIMIT :n"
)
# B7 item 3, class 3: finds made in tutor answers the owner labelled bad (their job traces, FR-OBS-5).
BAD_ANSWERS_SQL: Final = (
    "SELECT k.job_id, k.key FROM owner_labels l JOIN job_keys k ON k.kind = 'tutor.answer'"
    " AND k.args->>'question_id' = l.target_id::text AND k.job_id IS NOT NULL"
    " WHERE l.owner_id = :o AND l.target = 'answer' AND NOT l.ok"
)


def _group(route: str | None, packs: Sequence[str] | None) -> tuple[str, str]:
    return (route or "", (list(packs) or [""])[0] if packs else "")


async def _bad_traces(conn: Any, owner: uuid.UUID) -> set[str]:
    from reader.obs.ids import derive_trace_id

    rows = (await conn.execute(text(BAD_ANSWERS_SQL), {"o": owner})).all()
    return {f"{derive_trace_id(int(r.job_id), str(r.key)):032x}" for r in rows}


async def queue(conn: Any, owner: uuid.UUID, slot: str, limit: int = QUEUE_MAX) -> list[dict[str, Any]]:
    thresholds = THRESHOLDS()
    limit = max(1, min(int(limit), QUEUE_MAX))
    threshold = thresholds.get(slot)
    items: list[Item] = []
    if slot == FIND:
        bad = await _bad_traces(conn, owner)
        rows = (await conn.execute(text(INPUTS_SQL), {"o": owner, "below": DISAGREE_BELOW, "n": FETCH_MAX})).all()
        for r in rows:
            suff = (r.output or {}).get("sufficiency")
            near = threshold is not None and isinstance(suff, (int, float)) and abs(suff - threshold) <= NEAR
            klass = 1 if r.disagree else 2 if near else 3 if r.find_trace_id in bad else 4
            items.append(Item(f"ji:{r.id}", slot, r.created_at, r.route, r.channel,
                              _group(r.route, r.pack_ids), klass))
    else:
        rows = (await conn.execute(
            text(SCORES_SQL), {"o": owner, "slot": slot, "below": DISAGREE_BELOW, "n": FETCH_MAX})).all()
        for r in rows:
            near = threshold is not None and abs(float(r.value) - threshold) <= NEAR
            klass = 1 if r.disagree else 2 if near else 4
            items.append(Item(f"es:{r.id}", slot, r.created_at, r.route, r.channel,
                              _group(r.route, r.pack_ids), klass))
    return [
        {"id": i.id, "slot": i.slot, "created_at": i.created_at.isoformat(), "route": i.route,
         "channel": i.channel}
        for i in order(items, limit)
    ]


JI_SQL: Final = (
    "SELECT id, query, card_ids, pack_ids, output, judge, labelable, route, channel, created_at"
    " FROM judge_inputs WHERE id = :id"
)
ES_SQL: Final = (
    "SELECT s.id, s.slot, s.value, s.judge, s.judge_input_id, s.eval_id, s.record_id, r.set_doc, er.card_ids"
    " FROM obs_eval_scores s LEFT JOIN obs_eval_runs r ON r.id = s.eval_id"
    " LEFT JOIN obs_eval_records er ON er.eval_id = s.eval_id AND er.record_id = s.record_id"
    " WHERE s.id = :id AND s.owner_id = :o"
)


def _question(set_doc: Any, record_id: str | None) -> str | None:
    for q in (set_doc or {}).get("questions") or []:
        if isinstance(q, dict) and q.get("id") == record_id:
            return str(q.get("question"))
    return None


def _ids(card_ids: Sequence[str], ordinals: Any) -> list[str]:
    return [card_ids[n - 1] for n in ordinals or [] if isinstance(n, int) and 1 <= n <= len(card_ids)]


async def _cards(conn: Any, owner: uuid.UUID, card_ids: list[str]) -> list[dict[str, Any]]:
    views = await CARDS.read(conn, owner, card_ids)
    return [{"id": v.id, "text": v.text, "view": v.view} for v in views]


async def item(conn: Any, owner: uuid.UUID, raw: str) -> dict[str, Any] | None:
    """The item as the owner may read it; None when missing or expired; NotLabelable for 409."""
    parsed = parse_item(raw)
    if parsed is None:
        return None
    kind, ident = parsed
    if kind == "ji":
        row = (await conn.execute(text(JI_SQL), {"id": ident})).first()
        if row is None:
            return None
        if not row.labelable:
            raise NotLabelable(raw)
        ids = list(row.card_ids)
        output = row.output or {}
        return {"id": raw, "slot": FIND, "query": row.query, "judge": row.judge,
                "output": {"keep": _ids(ids, output.get("keep")), "drop": _ids(ids, output.get("drop")),
                           "sufficiency": output.get("sufficiency")},
                "cards": await _cards(conn, owner, ids)}
    found = (await conn.execute(text(ES_SQL), {"id": ident, "o": owner})).first()
    if found is None:
        return None
    if found.judge_input_id is not None:
        source = (await conn.execute(text(JI_SQL), {"id": found.judge_input_id})).first()
        if source is None:
            return None
        if not source.labelable:
            raise NotLabelable(raw)
        query, ids = source.query, list(source.card_ids)
    else:
        query, ids = _question(found.set_doc, found.record_id), list(found.card_ids or [])
        if not await CARDS.resolvable(conn, ids):
            raise NotLabelable(raw)
    return {"id": raw, "slot": found.slot, "query": query, "judge": found.judge, "value": float(found.value),
            "cards": await _cards(conn, owner, ids)}
