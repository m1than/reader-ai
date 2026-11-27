"""Shared [db] helpers for the ADR-0039 SRV-OBS tests (V.kl-obs-spans): a migrated engine, users, a
`judge_requests`-shaped request and the judge_outputs stand-in SRV-AGENT's migration adds on its own branch.

@trace FR-OBS-38 FR-OBS-39 FR-OBS-40
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from jobs.conftest import target
from reader.db import make_engine, migrate

# SRV-AGENT's agent_0004 creates judge_outputs on origin/V/kl-agent-judges; until both branches meet, the tests
# create the columns the sink reads.
JUDGE_OUTPUTS_SQL = (
    "CREATE TABLE IF NOT EXISTS judge_outputs (owner_id uuid NOT NULL, input_sha256 text NOT NULL,"
    " judge text NOT NULL, output jsonb NOT NULL, pack_ids text[] NOT NULL DEFAULT '{}',"
    " created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (owner_id, input_sha256, judge))"
)
JUDGE = "judge.find@" + "a" * 12


async def engine_for(pg: Any) -> Any:
    engine = make_engine(target(pg))
    await migrate(engine)
    async with engine.begin() as conn:
        await conn.execute(text(JUDGE_OUTPUTS_SQL))
    return engine


async def user(engine: Any, role: str = "learner", consent: bool = False) -> uuid.UUID:
    from reader.auth.handles import skeleton

    handle = "u" + secrets.token_hex(6)
    async with engine.begin() as conn:
        found = await conn.execute(
            text(
                "INSERT INTO users (handle, role, webauthn_user_id, display_name, consent_evaluation,"
                " handle_skeleton) VALUES (:h, :r, :w, 'Test', :c, :k) RETURNING id"
            ),
            {"h": handle, "r": role, "w": secrets.token_bytes(64), "c": consent, "k": skeleton(handle)},
        )
        return uuid.UUID(str(found.scalar_one()))


@dataclass(frozen=True)
class Request:
    """The fields of SRV-AGENT's JudgeRequest the sink reads."""

    owner_id: uuid.UUID
    query: str
    cards: tuple[tuple[str, str], ...]
    pack_ids: tuple[str, ...] = ("p" * 64,)
    line: str = "tutor"
    request_id: uuid.UUID = field(default_factory=uuid.uuid4)


CARDS = (
    ("spn:" + "1" * 64 + ":p10:0-120", "card one text"),
    ("spn:" + "1" * 64 + ":p11:5-90", "card two text"),
    ("spn:" + "1" * 64 + ":p12:0-40", "card three text"),
)


async def record_output(conn: Any, request: Request, output: dict[str, Any], judge: str = JUDGE) -> str:
    """What the lane's write transaction does before the sink: the recorded output row."""
    import json

    from reader.agent.proxy import canonical_sha256

    sha = canonical_sha256(
        {"owner": str(request.owner_id), "query": request.query, "cards": [[i, t] for i, t in request.cards]}
    )
    await conn.execute(
        text(
            "INSERT INTO judge_outputs (owner_id, input_sha256, judge, output, pack_ids)"
            " VALUES (:o, :h, :j, CAST(:out AS jsonb), CAST(:p AS text[]))"
        ),
        {"o": request.owner_id, "h": sha, "j": judge, "out": json.dumps(output), "p": list(request.pack_ids)},
    )
    return str(sha)
