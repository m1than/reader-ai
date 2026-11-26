"""Judge slots, ledger lines, judge versions and recorded outputs (SRV-AGENT FR-AGENT-18..20, -22).

Slots and lines are data (ADR-0039 B7). A judge is `<slot>@<version>`, the version a hash of its prompt bytes,
model and output schema. Recorded outputs are owner-scoped rows keyed (owner, input hash, judge), written
insert-or-ignore and never updated; they hold card ids and numbers, never text.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from reader.agent.errors import agent_error
from reader.agent.proxy import canonical_sha256
from reader.config import ReaderConfig

PROMPTS: Final = Path(__file__).parent / "prompts"
FIND: Final = "judge.find"
FIND_SCHEMA_VERSION: Final = 1
# The one instructions section of a slot prompt (FR-AGENT-18; SRV-OBS's delimiter): alignment replaces only
# the text between them. Data never carries the marker (render_find_input neutralises it).
INSTRUCTIONS_MARK: Final = "READER-INSTRUCTIONS"
INSTRUCTIONS_BEGIN: Final = "<<<" + INSTRUCTIONS_MARK
INSTRUCTIONS_END: Final = INSTRUCTIONS_MARK + ">>>"
NEUTRAL_MARK: Final = "READER_INSTRUCTIONS_DATA"


@dataclass(frozen=True)
class Slot:
    name: str
    line: str | None  # None: the caller gives it (judge.find)
    lane: str  # "worker-judge" | "worker"
    v1: str  # the v1 model binding (an alias)
    max_output_tokens: int | None = None
    floor_usd: Decimal | None = None
    prompt: str | None = None  # a file under prompts/; None: the owning module passes its prompt bytes


EVAL_NAMES: Final = ("relevance", "sufficiency", "groundedness", "self_contained", "citation_supports_claim")
JUDGE_SLOTS: Final[Mapping[str, Slot]] = {
    s.name: s
    for s in (
        Slot(FIND, None, "worker-judge", "haiku", 512, Decimal("0.002"), "judge_find.md"),
        Slot("judge.link_type", "index", "worker", "haiku"),
        Slot("ingest.context_prefix", "index", "worker", "haiku"),
        *(Slot(f"judge.eval.{n}", "eval", "worker", "opus") for n in EVAL_NAMES),
        Slot("judge.align", "eval", "worker", "opus"),
    )
}
TASK_LINES: Final[Mapping[str, str]] = {
    "draft": "compile",
    "fact_check": "compile",
    "review": "compile",
    "tutor": "tutor",
    "freshness_scan": "freshness",
    "freshness_web": "freshness",
    "freshness_report": "freshness",
    **{s.name: s.line for s in JUDGE_SLOTS.values() if s.line is not None},
}
FIND_LINES: Final = frozenset({"tutor", "compile", "mcp", "eval"})
LANE_LINES: Final = frozenset({"tutor", "compile", "mcp"})  # FR-AGENT-21: eval never comes through the lane
CAP_KEYS: Final = ("index_usd_per_pack", "index_usd_per_pair_run", "eval_usd_per_run")
SCOPE_KINDS: Final = frozenset({"overlay", "pair_run", "eval_run"})
_NAMESPACE: Final = uuid.uuid5(uuid.NAMESPACE_URL, "reader-ai/agent/judges")


def line_for(task: str, line: str | None) -> str | None:
    """The call's stored line (FR-AGENT-19), or `line_invalid`."""
    if task == FIND:
        if line not in FIND_LINES:
            raise agent_error("line_invalid")
        return line
    fixed = TASK_LINES.get(task)
    if line is not None and line != fixed:
        raise agent_error("line_invalid")
    return fixed


def cap_key(task: str, line: str | None) -> str | None:
    """The configured line cap a call runs under, if any (FR-AGENT-19)."""
    if task == "ingest.context_prefix":
        return "index_usd_per_pack"
    if task == "judge.link_type":
        return "index_usd_per_pair_run"
    if line == "eval":
        return "eval_usd_per_run"
    return None


def line_caps(config: ReaderConfig) -> Mapping[str, Decimal | None]:
    """The index and eval caps: all None (so `cap_missing`) until SRV-CONFIG's schema rows land and its paired
    change supplies them through `reader.config.effective`, the only reader of overridable keys."""
    return dict.fromkeys(CAP_KEYS)


def scope_run_id(owner_id: uuid.UUID, kind: str, version_id: str) -> uuid.UUID:
    """An owner-unique run id for a line cap's scope (FR-AGENT-19)."""
    if kind not in SCOPE_KINDS:
        raise ValueError(kind)
    return uuid.uuid5(_NAMESPACE, f"{kind}:{owner_id}:{version_id}")


def prompt_bytes(slot: str) -> bytes:
    name = JUDGE_SLOTS[slot].prompt
    if name is None:
        raise LookupError(slot)  # its owning module passes the prompt bytes
    return (PROMPTS / name).read_bytes()


def judge_version(prompt: bytes, model: str, schema_version: int) -> str:
    digest = hashlib.sha256()
    for part in (prompt, model.encode("utf-8"), str(schema_version).encode("ascii")):
        digest.update(hashlib.sha256(part).digest())
    return digest.hexdigest()[:12]


def judge_name(slot: str, prompt: bytes, model: str, schema_version: int) -> str:
    return f"{slot}@{judge_version(prompt, model, schema_version)}"


def find_judge(model: str) -> str:
    return judge_name(FIND, prompt_bytes(FIND), model, FIND_SCHEMA_VERSION)


def find_input_sha256(owner_id: uuid.UUID, query: str, cards: Sequence[tuple[str, str]]) -> str:
    return canonical_sha256({"owner": str(owner_id), "query": query, "cards": [[i, t] for i, t in cards]})


def neutralise(data: str) -> str:
    """A line of data that carries an instructions delimiter gets the token replaced (FR-AGENT-22)."""
    lines = data.split("\n")
    return "\n".join(
        line.replace(INSTRUCTIONS_MARK, NEUTRAL_MARK)
        if INSTRUCTIONS_BEGIN in line or INSTRUCTIONS_END in line
        else line
        for line in lines
    )


def render_find_input(query: str, cards: Sequence[tuple[str, str]]) -> str:
    """The user message: the question and the cards by ordinal, neutralised; no card id reaches the model."""
    listed = "\n\n".join(f"[{n}] {neutralise(body)}" for n, (_id, body) in enumerate(cards, start=1))
    return f"Question:\n{neutralise(query)}\n\nCards:\n{listed}\n"


# --- the judge.find output (FR-AGENT-22, VEC-AGENT-5) ---

_FENCE: Final = re.compile(r"\A```json\n(.*)\n```\Z", re.DOTALL)


def _no_constant(_name: str) -> Any:
    raise ValueError("non-finite number")


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _ordinals(value: Any, n: int) -> list[int]:
    if not isinstance(value, list) or not all(type(v) is int and 1 <= v <= n for v in value):
        raise agent_error("output_invalid")
    if len(set(value)) != len(value):
        raise agent_error("output_invalid")
    return value


def parse_find_output(raw: str, card_ids: Sequence[str]) -> dict[str, Any]:
    """`{"keep": [ids], "drop": [ids], "sufficiency": x}` from the model's text, or `output_invalid`."""
    body = raw.strip()
    fenced = _FENCE.match(body)
    if fenced is not None:
        body = fenced.group(1)
    try:
        data = json.loads(body, parse_constant=_no_constant, object_pairs_hook=_unique_keys)
    except ValueError:
        raise agent_error("output_invalid") from None
    keys = set(data) if isinstance(data, dict) else set()
    if not isinstance(data, dict) or not {"keep", "sufficiency"} <= keys <= {"keep", "drop", "sufficiency"}:
        raise agent_error("output_invalid")
    n = len(card_ids)
    keep = _ordinals(data["keep"], n)
    rest = [o for o in range(1, n + 1) if o not in keep]
    if "drop" in data:
        drop = _ordinals(data["drop"], n)
        if sorted(drop) != rest:
            raise agent_error("output_invalid")
    else:
        drop = rest
    suff = data["sufficiency"]
    # The range first: an integer like 10**400 is too large for float, and isfinite would raise OverflowError.
    if type(suff) not in (int, float) or not 0 <= suff <= 1 or not math.isfinite(suff):
        raise agent_error("output_invalid")
    return {
        "keep": [card_ids[o - 1] for o in keep],
        "drop": [card_ids[o - 1] for o in drop],
        "sufficiency": suff,
    }


# --- recorded outputs (FR-AGENT-20) ---


async def record(
    conn: AsyncConnection,
    owner_id: uuid.UUID,
    input_sha256: str,
    judge: str,
    output: Mapping[str, Any],
    pack_ids: Sequence[str] = (),
    overlay_id: str | None = None,
    request_id: uuid.UUID | None = None,
) -> bool:
    """Insert-or-ignore; True when this call inserted the row."""
    row = await conn.execute(
        text(
            "INSERT INTO judge_outputs (owner_id, input_sha256, judge, output, pack_ids, overlay_id,"
            " request_id) VALUES (:o, :h, :j, CAST(:out AS jsonb), CAST(:p AS text[]), :ov, :rq)"
            " ON CONFLICT DO NOTHING RETURNING 1"
        ),
        {
            "o": owner_id,
            "h": input_sha256,
            "j": judge,
            "out": json.dumps(dict(output), ensure_ascii=False),
            "p": list(pack_ids),
            "ov": overlay_id,
            "rq": request_id,
        },
    )
    return row.first() is not None


async def recorded(
    conn: AsyncConnection, owner_id: uuid.UUID, input_sha256: str, judge: str
) -> dict[str, Any] | None:
    value = (
        await conn.execute(
            text("SELECT output FROM judge_outputs WHERE owner_id = :o AND input_sha256 = :h AND judge = :j"),
            {"o": owner_id, "h": input_sha256, "j": judge},
        )
    ).scalar()
    if value is None:
        return None
    data: dict[str, Any] = json.loads(value) if isinstance(value, str) else dict(value)
    return data


# The only DELETEs the trigger allows, as fixed statements (no SQL is built from input).
_EXPIRE: Final = (
    "DELETE FROM judge_outputs WHERE judge LIKE 'judge.find@%'"
    " AND created_at < now() - make_interval(days => :d)"
)
_FORGET_PACK: Final = (
    "DELETE FROM judge_outputs WHERE owner_id = :o AND judge LIKE 'judge.find@%' AND :p = ANY(pack_ids)"
)
_FORGET_OVERLAY: Final = "DELETE FROM judge_outputs WHERE owner_id = :o AND overlay_id = :v"


async def _delete(conn: AsyncConnection, statement: str, params: Mapping[str, Any]) -> int:
    """A DELETE the trigger allows: the setting is on for this statement only. No `finally`: a failed DELETE
    aborts the transaction, which ends the transaction-local setting too, and its error is the one raised."""
    await conn.execute(text("SELECT set_config('reader.judge_delete', 'on', true)"))
    result = await conn.execute(text(statement), dict(params))
    await conn.execute(text("SELECT set_config('reader.judge_delete', 'off', true)"))
    count: int = result.rowcount
    return count


async def expire(conn: AsyncConnection, days: int = 30) -> int:
    """judge.find rows older than the retention (bounds.judge_find_output_retention_days)."""
    return await _delete(conn, _EXPIRE, {"d": days})


async def forget_pack(conn: AsyncConnection, owner_id: uuid.UUID, pack_id: str) -> int:
    return await _delete(conn, _FORGET_PACK, {"o": owner_id, "p": pack_id})


async def forget_overlay(conn: AsyncConnection, owner_id: uuid.UUID, overlay_id: str) -> int:
    return await _delete(conn, _FORGET_OVERLAY, {"o": owner_id, "v": overlay_id})


class ReplayProvider:
    """Recorded outputs for one owner and one input (tests and rebuilds): no model call, ever."""

    def __init__(self, engine: AsyncEngine, owner_id: uuid.UUID, input_sha256: str) -> None:
        self.engine, self.owner_id, self.input_sha256 = engine, owner_id, input_sha256

    async def run(self, judge: str) -> dict[str, Any]:
        async with self.engine.connect() as conn:
            output = await recorded(conn, self.owner_id, self.input_sha256, judge)
        if output is None:
            raise agent_error("replay_miss")
        return output
