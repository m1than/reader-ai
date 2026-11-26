"""`judge_requests`: the transport of a `judge.find` query and its card text (SRV-EVENTS FR-EVENTS-33, ADR-0039 B7b).

The waiter writes a row with `put` and commits, then enqueues a `judge.find` job whose args carry only the request id
and the input hash; the lane's handler reads the row (`get`) and deletes it at its end (`delete`); the collectors
delete rows past their deadline (`expire`, FR-EVENTS-34). Nothing here logs a query or a card text.

`reader.agent.lane` imports this module for its `JOB_KINDS`; this module imports `reader.agent.lane` (its record and
window bounds) only inside the functions that need it, so the two never form an import cycle.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from reader.jobs.model import JobError

if TYPE_CHECKING:
    from reader.agent.lane import JudgeRequest

LINES: Final = frozenset({"tutor", "compile", "mcp"})  # the CHECK of jobs_0004; eval never uses the lane
GRACE_S: Final = 60  # bounds: a row is collected once its deadline is this far behind
_HEX64: Final = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class NewJudgeRequest:
    """What the waiter writes; the database sets `created_at`."""

    request_id: uuid.UUID
    owner_id: uuid.UUID
    line: str
    run_id: uuid.UUID
    run_cap_usd: Decimal
    lesson_id: uuid.UUID | None
    daily_budget_usd: Decimal | None
    input_sha256: str
    deadline: datetime
    query: str
    cards: tuple[tuple[str, str], ...]
    pack_ids: tuple[str, ...]


class JudgeRequestInvalid(JobError):
    """`judge_request_invalid`, naming the refused field only (never its value)."""

    def __init__(self, field: str) -> None:
        super().__init__("judge_request_invalid")
        self.field = field


def _invalid(field: str) -> JobError:
    return JudgeRequestInvalid(field)


def _check(new: NewJudgeRequest) -> None:
    if new.line not in LINES:
        raise _invalid("line")
    if not new.cards or not all(
        isinstance(c, tuple | list) and len(c) == 2 and all(isinstance(x, str) for x in c) for c in new.cards
    ):
        raise _invalid("cards")
    if not all(isinstance(p, str) for p in new.pack_ids):
        raise _invalid("pack_ids")
    if not isinstance(new.input_sha256, str) or not _HEX64.fullmatch(new.input_sha256):
        raise _invalid("input_sha256")
    if not isinstance(new.query, str):
        raise _invalid("query")


class Store:
    """The production `JudgeRequests` of SRV-AGENT's lane (get, delete), plus the waiter's `put`."""

    async def put(self, conn: AsyncConnection, new: NewJudgeRequest) -> None:
        from reader.agent.lane import JUDGE_KILL_FACTOR, LANE_TOKEN_TTL_S, LANE_TTL_MARGIN_S

        _check(new)
        # One statement: its now() is created_at, the deadline check and the lane's window check
        # (FR-AGENT-21 step 4: factor x (deadline - created) <= ttl - margin), so the lane never refuses it.
        inserted = await conn.execute(
            text(
                "INSERT INTO judge_requests (request_id, owner_id, line, run_id, run_cap_usd, lesson_id,"
                " daily_budget_usd, input_sha256, deadline, query, cards, pack_ids)"
                " SELECT :rid, :owner, :line, :run, :cap, :lesson, :daily, :sha, :deadline, :query,"
                " CAST(:cards AS jsonb), CAST(:packs AS text[])"
                " WHERE :deadline > now()"
                " AND :factor * extract(epoch FROM (:deadline - now())) <= :window"
            ),
            {
                "rid": new.request_id,
                "owner": new.owner_id,
                "line": new.line,
                "run": new.run_id,
                "cap": new.run_cap_usd,
                "lesson": new.lesson_id,
                "daily": new.daily_budget_usd,
                "sha": new.input_sha256,
                "deadline": new.deadline,
                "query": new.query,
                "cards": json.dumps([list(c) for c in new.cards], ensure_ascii=False),
                "packs": list(new.pack_ids),
                "factor": JUDGE_KILL_FACTOR,
                "window": LANE_TOKEN_TTL_S - LANE_TTL_MARGIN_S,
            },
        )
        if inserted.rowcount != 1:
            raise _invalid("deadline")

    async def get(self, conn: AsyncConnection, request_id: uuid.UUID) -> JudgeRequest | None:
        from reader.agent.lane import JudgeRequest

        row = (
            await conn.execute(
                text(
                    "SELECT request_id, owner_id, line, run_id, run_cap_usd, lesson_id, daily_budget_usd,"
                    " created_at, deadline, query, cards::text, pack_ids FROM judge_requests WHERE request_id = :r"
                ),
                {"r": request_id},
            )
        ).first()
        if row is None:
            return None
        cards: Any = json.loads(row[10])
        return JudgeRequest(
            request_id=row[0],
            owner_id=row[1],
            line=row[2],
            run_id=row[3],
            run_cap_usd=row[4],
            lesson_id=row[5],
            daily_budget_usd=row[6],
            created_at=row[7],
            deadline=row[8],
            query=row[9],
            cards=tuple((str(c[0]), str(c[1])) for c in cards),
            pack_ids=tuple(row[11]),
        )

    async def delete(self, conn: AsyncConnection, request_id: uuid.UUID) -> None:
        await conn.execute(text("DELETE FROM judge_requests WHERE request_id = :r"), {"r": request_id})


STORE: Final = Store()


async def expire(conn: AsyncConnection) -> int:
    """Delete every row whose deadline is more than `GRACE_S` behind the database's clock (FR-EVENTS-34)."""
    result = await conn.execute(
        text("DELETE FROM judge_requests WHERE deadline + make_interval(secs => :g) < now()"), {"g": GRACE_S}
    )
    return int(result.rowcount or 0)
