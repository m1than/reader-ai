"""SRV-OBS FR-OBS-38 (ADR-0039 B7 labels item 2, B7b): the `judge_inputs` store.

`SINK` implements SRV-AGENT's `JudgeInputsSink.copy(conn, request_row, output)`: the `judge.find` job calls it
in the transaction that recorded the output, only when that insert inserted. It stores ids, hashes, the output
as 1-based ordinals into the request's card ids, and the query only for a consenting caller; card text never.
A copy that fails validation, or any database error inside it, is skipped in a savepoint with one log line (a
code, never a value): it never aborts the recorded output and never raises. Trace ids come from the current
span.
"""

from __future__ import annotations

import json
import logging
import math
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Final

from opentelemetry import trace
from sqlalchemy import text

log = logging.getLogger("reader.obs")
SLOT: Final = "judge.find"
MAX_CARDS: Final = 50
EVAL_LINE: Final = "eval"
OUTPUT_KEYS: Final = frozenset({"keep", "drop", "sufficiency"})

CALLER_SQL: Final = "SELECT role, consent_evaluation FROM users WHERE id = :o"
# The judge string of the output the lane has just recorded in this transaction (SRV-AGENT FR-AGENT-20).
JUDGE_SQL: Final = (
    "SELECT judge FROM judge_outputs WHERE owner_id = :o AND input_sha256 = :h AND judge LIKE 'judge.find@%'"
    " ORDER BY created_at DESC LIMIT 1"
)
INSERT_SQL: Final = (
    "INSERT INTO judge_inputs (id, owner_id, slot, input_sha256, judge, query, card_ids, pack_ids, output,"
    " find_trace_id, judge_trace_id, labelable) VALUES (:id, :o, :slot, :h, :j, :q, CAST(:c AS text[]),"
    " CAST(:p AS text[]), CAST(:out AS jsonb), :ft, :jt, :lab)"
)
EXPIRE_SQL: Final = "DELETE FROM judge_inputs WHERE created_at < now() - make_interval(days => :d)"
PURGE_SQL: Final = "DELETE FROM judge_inputs WHERE :p = ANY (pack_ids)"
FORGET_SQL: Final = (
    "UPDATE judge_inputs SET query = NULL, labelable = false WHERE owner_id = :o AND query IS NOT NULL"
)


class Skip(Exception):
    """A copy that must not be written; `code` is logged, never a value."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _ordinal_list(value: object, index: Mapping[str, int]) -> list[int]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise Skip("shape")
    if len(set(value)) != len(value):
        raise Skip("duplicate")
    if any(v not in index for v in value):
        raise Skip("unknown_card")
    return [index[v] for v in value]


def ordinals(card_ids: Sequence[str], output: Mapping[str, Any]) -> dict[str, Any]:
    """SRV-AGENT's parsed output (FR-AGENT-22: card ids) as 1-based ordinals into `card_ids`."""
    if len(card_ids) > MAX_CARDS or not card_ids:
        raise Skip("too_many_cards" if card_ids else "shape")
    if not isinstance(output, Mapping) or not {"keep", "sufficiency"} <= set(output) <= OUTPUT_KEYS:
        raise Skip("shape")
    index = {card: n for n, card in enumerate(card_ids, start=1)}
    keep = _ordinal_list(output["keep"], index)
    drop = (
        _ordinal_list(output["drop"], index)
        if "drop" in output
        else [n for n in range(1, len(card_ids) + 1) if n not in keep]
    )
    if set(keep) & set(drop):
        raise Skip("duplicate")
    suff = output["sufficiency"]
    if suff is not None and (type(suff) not in (int, float) or not math.isfinite(suff) or not 0 <= suff <= 1):
        raise Skip("sufficiency")
    return {"keep": keep, "drop": drop, "sufficiency": None if suff is None else float(suff)}


def consenting(role: str, consent_evaluation: bool) -> bool:
    """B7 labels item 2: the instance owner's own queries, and other users' with evaluation consent."""
    return role == "owner" or bool(consent_evaluation)


def current_trace_ids() -> tuple[str | None, str | None]:
    """(the current span's trace id, the trace id of its first valid link); None where absent."""
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if not ctx.is_valid:
        return None, None
    found = None
    for link in getattr(span, "links", ()) or ():
        linked = getattr(link, "context", None)
        if linked is not None and linked.is_valid:
            found = f"{linked.trace_id:032x}"
            break
    return f"{ctx.trace_id:032x}", found


def input_sha256(owner_id: uuid.UUID, query: str, cards: Sequence[tuple[str, str]]) -> str:
    """SRV-AGENT's `find_input_sha256` (the same canonical form)."""
    from reader.agent.proxy import canonical_sha256

    payload = {"owner": str(owner_id), "query": query, "cards": [[i, t] for i, t in cards]}
    return str(canonical_sha256(payload))


class Sink:
    """SRV-AGENT's `JudgeInputsSink` for the `judge_inputs` copy."""

    async def copy(self, conn: Any, request: Any, output: Mapping[str, Any]) -> None:
        if getattr(request, "line", None) == EVAL_LINE:
            return  # alignment re-runs never reach the lane (FR-OBS-41); nothing of the eval line is copied
        try:
            cards = [(str(i), str(t)) for i, t in request.cards]
            shaped = ordinals([i for i, _ in cards], output)
            async with conn.begin_nested():
                await self._insert(conn, request, cards, shaped)
        except Skip as skipped:
            log.info("obs judge_inputs_skipped code=%s", skipped.code)
        except Exception as error:  # a database error inside the savepoint: the output insert stays
            log.info("obs judge_inputs_skipped code=%s", type(error).__name__)

    async def _insert(
        self, conn: Any, request: Any, cards: list[tuple[str, str]], shaped: Mapping[str, Any]
    ) -> None:
        owner = request.owner_id
        sha = input_sha256(owner, request.query, cards)
        judge = (await conn.execute(text(JUDGE_SQL), {"o": owner, "h": sha})).scalar()
        if judge is None:
            raise Skip("judge_unknown")
        caller = (await conn.execute(text(CALLER_SQL), {"o": owner})).first()
        if caller is None:
            raise Skip("caller_unknown")
        consent = consenting(str(caller.role), bool(caller.consent_evaluation))
        judge_trace, find_trace = current_trace_ids()
        await conn.execute(
            text(INSERT_SQL),
            {
                "id": uuid.uuid4(),
                "o": owner,
                "slot": SLOT,
                "h": sha,
                "j": str(judge),
                "q": request.query if consent else None,
                "c": [i for i, _ in cards],
                "p": [str(p) for p in request.pack_ids],
                "out": json.dumps(dict(shaped)),
                "ft": find_trace,
                "jt": judge_trace,
                "lab": consent,
            },
        )


SINK: Final = Sink()


async def delete_expired(conn: Any, retention_days: int) -> int:
    """FR-OBS-38 / FR-OBS-11 as amended: rows older than `retention_days`."""
    return int((await conn.execute(text(EXPIRE_SQL), {"d": int(retention_days)})).rowcount)


async def purge_pack(conn: Any, pack_id: str) -> int:
    """Every row citing the pack (SRV-INGEST's purge calls it under the per-pack locks)."""
    return int((await conn.execute(text(PURGE_SQL), {"p": pack_id})).rowcount)


async def forget_queries(conn: Any, user_id: uuid.UUID) -> int:
    """A withdrawal of `consent_evaluation` (paired SRV-AUTH): that caller's stored queries become NULL."""
    return int((await conn.execute(text(FORGET_SQL), {"o": user_id})).rowcount)
