"""SRV-OBS FR-OBS-39 (ADR-0039 B9; ADR-0038 amended under its `revisit_when`; ADR-0035 Decision 1 amended):
local judge evaluation.

Judges that read text run in the worker; only scores reach MLflow, as LLM_JUDGE assessments posted by
`obs.judge_score` (FR-OBS-14). `start` is the one entry (the owner route and the OPS-CLI host command call
it). `obs.eval` scores one record (max one attempt: no judge call is ever repeated), `obs.eval_run` closes a
run (the last record's check 0, or the deadline check) and writes its Run through FR-OBS-20's steps,
`obs.eval_sample` starts a sample run. Ports (`PORTS`) stand for SRV-SEARCH's find and card reader and
SRV-AGENT's eval-line Provider; their defaults refuse with NotWired until those land on this branch. Model
text is dropped in memory.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field
from ruamel.yaml import YAML
from sqlalchemy import text

from reader.jobs.model import JobKind
from reader.obs import phase2
from reader.obs.phase2 import MlflowRefused, request

log = logging.getLogger("reader.obs")
KIND: Final = "obs.eval"
KIND_RUN: Final = "obs.eval_run"
KIND_SAMPLE: Final = "obs.eval_sample"
KIND_SCORE: Final = "obs.judge_score"
EVAL_SLOTS: Final = (
    "judge.eval.relevance", "judge.eval.sufficiency", "judge.eval.groundedness", "judge.eval.self_contained",
    "judge.eval.citation_supports_claim",
)
FACT_SLOTS: Final = frozenset({"judge.eval.sufficiency", "judge.eval.groundedness"})
PROFILES: Final = ("mcp", "tutor")
QUESTION_TYPES: Final = ("exact", "thematic", "fact_check")
EVAL_SETS: Final[dict[str, dict[str, Any]]] = phase2.VEC["eval_sets"]
RECORD_S: Final = 180  # initial value: the deadline allows each record its job timeout
DEADLINE_EXTRA_S: Final = 600  # initial value
SAMPLE_MAX: Final = 20  # initial value
SCORE_SCHEDULE_S: Final = 60
UUID_RE: Final = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


class StartRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class NotWired(Exception):
    """A port whose owning module (SRV-SEARCH, SRV-AGENT) has not landed on this branch."""


class EvalCapReached(Exception):
    """The `eval` line's cap refused the call (SRV-AGENT FR-AGENT-19)."""


class ScoreRefused(Exception):
    """MLflow answered 4xx other than 404 to a score post: the key ends dead."""


class TraceMissing(Exception):
    """404 for the score's trace: transient while attempts remain."""


@dataclass(frozen=True)
class FindResult:
    card_ids: list[str]
    pack_ids: list[str]
    tokens: int
    judged_tokens: int
    unjudged_tokens: int
    judge_ran: bool | None
    judge_skip: str | None
    gate: str | None
    find_ms: int


Views = list[tuple[str, str | None]]  # (card id, text the payer may read under B8, or None)


class EvalPorts(Protocol):
    async def find(self, conn: Any, owner: uuid.UUID, question: str, profile: str) -> FindResult: ...

    async def cards(self, conn: Any, owner: uuid.UUID, card_ids: list[str]) -> Views: ...

    async def judge(
        self, slot: str, system: str, user_message: str, run_id: uuid.UUID, owner: uuid.UUID
    ) -> str: ...

    def model(self, slot: str) -> str: ...

    def versions(self) -> tuple[str | None, str | None, str | None]: ...


class Unwired:
    def model(self, slot: str) -> str:
        raise NotWired("SRV-AGENT slot binding")

    async def find(self, conn: Any, owner: uuid.UUID, question: str, profile: str) -> FindResult:
        raise NotWired("SRV-SEARCH find")

    async def cards(self, conn: Any, owner: uuid.UUID, card_ids: list[str]) -> Views:
        raise NotWired("SRV-SEARCH card reader")

    async def judge(
        self, slot: str, system: str, user_message: str, run_id: uuid.UUID, owner: uuid.UUID
    ) -> str:
        raise NotWired("SRV-AGENT eval line")

    def versions(self) -> tuple[str | None, str | None, str | None]:
        return None, None, None


PORTS: EvalPorts = Unwired()


def tracing_on() -> bool:
    return phase2.tracing_on()


# --- start ---


def eval_budget() -> tuple[Decimal | None, Mapping[str, Decimal]]:
    """The `eval` cap per run (reader.yaml `budgets.eval_usd_per_run`, SRV-CONFIG paired) and each slot's
    floor reserve (SRV-AGENT FR-AGENT-9). Until SRV-CONFIG's schema carries the cap, it is None: every start
    is refused `cap_missing`, as SRV-AGENT refuses an eval-line call without one. Wiring sets `BUDGET`."""
    return BUDGET


BUDGET: tuple[Decimal | None, Mapping[str, Decimal]] = (None, {})


def _questions(set_text: str) -> list[dict[str, Any]]:
    loaded = YAML(typ="safe").load(set_text) or {}
    found = loaded.get("questions") if isinstance(loaded, dict) else None
    return [q for q in found or [] if isinstance(q, dict) and isinstance(q.get("id"), str)]


def check_start(
    budget_profile: str,
    judges: Sequence[str],
    *,
    set_text: str | None = None,
    sample_n: int = 0,
    cap_usd: Decimal | None,
    floors: Mapping[str, Decimal],
) -> tuple[str | None, list[dict[str, Any]]]:
    """(set name, records) or StartRefused, before any row."""
    name: str | None = None
    records: list[dict[str, Any]] = []
    if set_text is not None:
        sha = hashlib.sha256(set_text.encode("utf-8")).hexdigest()
        name = next((n for n, e in EVAL_SETS.items() if e.get("sha256") and e["sha256"] == sha), None)
        if name is None:
            raise StartRefused("set_not_pinned")
        records = _questions(set_text)
    if budget_profile not in PROFILES or not judges or any(j not in EVAL_SLOTS for j in judges):
        raise StartRefused("bad_request")
    if len(set(judges)) != len(judges):
        raise StartRefused("bad_request")
    if cap_usd is None or any(j not in floors for j in judges):
        raise StartRefused("cap_missing")  # no cap, or a slot without its floor reserve: fail closed
    count = len(records) if set_text is not None else sample_n
    estimate = sum((floors.get(j, Decimal(0)) for j in judges), Decimal(0)) * count
    if estimate > cap_usd:
        raise StartRefused("eval_cap")
    return name, records


RUN_SQL: Final = (
    "INSERT INTO obs_eval_runs (id, owner_id, mode, set_name, set_sha256, set_doc, budget_profile, judges,"
    " brain_version, policy_version, version_key, records, deadline, state) VALUES (:id, :o, :mode, :name,"
    " :sha, CAST(:doc AS jsonb), :profile, CAST(:judges AS text[]), :bv, :pv, :vk, :n, :deadline, 'queued')"
)
RECORD_SQL: Final = (
    "INSERT INTO obs_eval_records (eval_id, record_id, owner_id, judge_input_id, state)"
    " VALUES (:e, :r, :o, :ji, 'queued')"
)


async def start(
    conn: Any,
    owner_id: uuid.UUID,
    budget_profile: str,
    judges: Sequence[str],
    *,
    set_text: str | None = None,
    sample_ids: Sequence[uuid.UUID] | None = None,
    cap_usd: Decimal | None,
    floors: Mapping[str, Decimal],
    now: datetime | None = None,
) -> uuid.UUID:
    """FR-OBS-39 (a): exactly one of `set_text` (dataset) or `sample_ids` (judge_inputs ids). Rows and the
    outbox jobs are written in the caller's transaction."""
    if (set_text is None) == (sample_ids is None):
        raise StartRefused("bad_request")
    sample = [uuid.UUID(str(s)) for s in sample_ids or []]
    name, records = check_start(budget_profile, judges, set_text=set_text, sample_n=len(sample),
                                cap_usd=cap_usd, floors=floors)
    eval_id = uuid.uuid4()
    count = len(records) if set_text is not None else len(sample)
    deadline = (now or datetime.now(UTC)) + timedelta(seconds=count * RECORD_S + DEADLINE_EXTRA_S)
    brain, policy, key = PORTS.versions()
    await conn.execute(
        text(RUN_SQL),
        {
            "id": eval_id, "o": owner_id, "mode": "dataset" if set_text is not None else "sample",
            "name": name,
            "sha": hashlib.sha256(set_text.encode()).hexdigest() if set_text is not None else None,
            "doc": json.dumps({"questions": records}) if set_text is not None else None,
            "profile": budget_profile, "judges": list(judges), "bv": brain, "pv": policy, "vk": key,
            "n": count, "deadline": deadline,
        },
    )
    ids = [str(r["id"]) for r in records] if set_text is not None else [f"ji:{s}" for s in sample]
    for n, record_id in enumerate(ids):
        await conn.execute(
            text(RECORD_SQL),
            {"e": eval_id, "r": record_id, "o": owner_id, "ji": sample[n] if set_text is None else None},
        )
        await phase2.stage(conn, KIND_DECL, {"eval_id": str(eval_id), "record_no": n})
    await phase2.stage(conn, RUN_DECL, {"eval_id": str(eval_id), "check": 1})
    return eval_id


# --- one record ---


class EvalArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    eval_id: str = Field(pattern=UUID_RE)
    record_no: int = Field(ge=0, le=999)


LOAD_SQL: Final = (
    "SELECT r.id, r.owner_id, r.mode, r.set_doc, r.budget_profile, r.judges, r.state AS run_state,"
    " e.record_id, e.judge_input_id, e.state FROM obs_eval_runs r JOIN obs_eval_records e ON e.eval_id = r.id"
    " WHERE r.id = :e ORDER BY e.record_id OFFSET :n LIMIT 1"
)
INPUT_SQL: Final = (
    "SELECT query, card_ids, pack_ids, find_trace_id, judge_trace_id FROM judge_inputs WHERE id = :i"
)
DONE_SQL: Final = (
    "UPDATE obs_eval_records SET state = :s, card_ids = CAST(:c AS text[]), pack_ids = CAST(:p AS text[]),"
    " find_ms = :ms, cards = :cards, tokens = :t, judged_tokens = :jt, unjudged_tokens = :ut,"
    " judge_ran = :jr, judge_skip = :js, gate = :g, skipped_not_entitled = :skip, invalid_outputs = :inv,"
    " trace_id = :trace"
    " WHERE eval_id = :e AND record_id = :r AND state = 'queued'"
)
SCORE_SQL: Final = (
    "INSERT INTO obs_eval_scores (id, owner_id, eval_id, judge_input_id, record_id, slot, judge, value,"
    " trace_id) VALUES (:id, :o, :e, :ji, :r, :s, :j, :v, :t) ON CONFLICT DO NOTHING"
)
CAPPED_SQL: Final = "UPDATE obs_eval_records SET state = 'capped' WHERE eval_id = :e AND state = 'queued'"
RUN_CAPPED_SQL: Final = "UPDATE obs_eval_runs SET state = 'capped' WHERE id = :e"
LEFT_SQL: Final = "SELECT count(*) FROM obs_eval_records WHERE eval_id = :e AND state = 'queued'"
LOCK_SQL: Final = "SELECT pg_advisory_xact_lock(:k)"
RUNNING_SQL: Final = "UPDATE obs_eval_runs SET state = 'running' WHERE id = :e AND state = 'queued'"


def _lock_key(eval_id: str) -> int:
    from reader.jobs.model import lock_id

    return int(lock_id(f"obs.eval:{eval_id}"))


def _expected_facts(doc: Any, record_id: str) -> list[str]:
    for q in (doc or {}).get("questions") or []:
        if q.get("id") == record_id:
            return [str(f) for f in q.get("expected_facts") or []]
    return []


def _question(doc: Any, record_id: str) -> str:
    for q in (doc or {}).get("questions") or []:
        if q.get("id") == record_id:
            return str(q.get("question") or "")
    return ""


async def handle_record(args: EvalArgs, conn: Any, ctx: Any) -> None:
    from reader.obs import judges, prompts
    from reader.obs.ids import derive_trace_id

    await conn.execute(text(LOCK_SQL), {"k": _lock_key(args.eval_id)})
    row = (await conn.execute(text(LOAD_SQL), {"e": args.eval_id, "n": args.record_no})).first()
    if row is None or row.state != "queued" or row.run_state not in ("queued", "running"):
        return  # a record of a closed or capped run, or one already scored: no call
    await conn.execute(text(RUNNING_SQL), {"e": args.eval_id})
    owner = row.owner_id
    trace = f"{derive_trace_id(int(ctx.job_id), str(ctx.key)):032x}"
    facts: list[str] = []
    if row.mode == "dataset":
        question = _question(row.set_doc, row.record_id)
        facts = _expected_facts(row.set_doc, row.record_id)
        try:
            found = await PORTS.find(conn, owner, question, row.budget_profile)
        except Exception as failure:
            log.info("obs eval_record_failed code=%s", type(failure).__name__)
            await conn.execute(text(DONE_SQL), _done(args, row, "failed", None, 0, 0, trace))
            await _maybe_last(conn, args.eval_id)
            return
        card_ids, pack_ids = list(found.card_ids), list(found.pack_ids)
    else:
        source = (await conn.execute(text(INPUT_SQL), {"i": row.judge_input_id})).first()
        if source is None or source.query is None:
            await conn.execute(text(DONE_SQL), _done(args, row, "failed", None, 0, 0, None))
            await _maybe_last(conn, args.eval_id)
            return
        question, card_ids, pack_ids = str(source.query), list(source.card_ids), list(source.pack_ids)
        found = None
        trace = source.find_trace_id or source.judge_trace_id
    views = await PORTS.cards(conn, owner, card_ids)
    shown = [(cid, body) for cid, body in views if body is not None]  # B8: the payer's entitlement only
    skipped = len(views) - len(shown)
    invalid = 0
    state = "done"
    if shown:
        for slot in row.judges:
            system, data = judges.eval_messages(slot, prompts.section(slot), question, shown, facts)
            try:
                raw = await PORTS.judge(slot, system, data, uuid.UUID(args.eval_id), owner)
            except EvalCapReached:
                state = "capped"
                break
            except Exception as failure:  # a provider failure is no score and not an invalid output
                log.info("obs eval_judge_failed code=%s", type(failure).__name__)
                continue
            try:
                value = judges.parse_eval(raw)
            except judges.OutputInvalid:
                invalid += 1
                continue
            score_id = uuid.uuid4()
            await conn.execute(text(SCORE_SQL), {
                "id": score_id, "o": owner, "e": args.eval_id,
                "ji": row.judge_input_id, "r": row.record_id, "s": slot,
                "j": judges.judge_name(slot, PORTS.model(slot)),
                "v": value, "t": trace,
            })
            await phase2.stage(conn, SCORE_DECL, {"score_id": str(score_id)})
    params = _done(args, row, state, found, skipped, invalid, trace if row.mode == "dataset" else None)
    params.update({"c": card_ids, "p": pack_ids})
    await conn.execute(text(DONE_SQL), params)
    if state == "capped":
        await conn.execute(text(CAPPED_SQL), {"e": args.eval_id})
        await conn.execute(text(RUN_CAPPED_SQL), {"e": args.eval_id})
        log.warning("obs eval_capped")
    await _maybe_last(conn, args.eval_id)


def _done(
    args: EvalArgs,
    row: Any,
    state: str,
    found: FindResult | None,
    skipped: int,
    invalid: int,
    trace: str | None,
) -> dict[str, Any]:
    return {
        "e": args.eval_id, "r": row.record_id, "s": state, "c": [], "p": [],
        "ms": found.find_ms if found else None, "cards": len(found.card_ids) if found else 0,
        "t": found.tokens if found else 0, "jt": found.judged_tokens if found else 0,
        "ut": found.unjudged_tokens if found else 0, "jr": found.judge_ran if found else None,
        "js": found.judge_skip if found else None, "g": found.gate if found else None,
        "skip": skipped, "inv": invalid, "trace": trace,
    }


async def _maybe_last(conn: Any, eval_id: str) -> None:
    """The record that leaves none queued stages the run's check 0 (under the run's lock)."""
    if int((await conn.execute(text(LEFT_SQL), {"e": eval_id})).scalar_one()) == 0:
        await phase2.stage(conn, RUN_DECL, {"eval_id": eval_id, "check": 0})


# --- the run ---


class RunArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    eval_id: str = Field(pattern=UUID_RE)
    check: int = Field(ge=0, le=1000)


@dataclass(frozen=True)
class RecordRow:
    question_type: str | None
    find_ms: int | None
    tokens: int
    judge_ran: bool | None
    gate: str | None
    state: str
    skipped_not_entitled: int
    invalid_outputs: int


def _nearest(values: Sequence[float], q: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    return float(ordered[max(1, math.ceil(q * len(ordered))) - 1])


Typed = Sequence[tuple[str | None, float]]  # (question type, score)


def metrics(records: Sequence[RecordRow], scores: Mapping[str, Typed]) -> dict[str, Any]:
    """FR-OBS-39 (c): the Run's metrics, by code, from the Postgres rows (VEC-OBS-5 `runs.eval.metrics`)."""
    done = [r for r in records if r.state == "done"]
    out: dict[str, Any] = {
        "records": len(records),
        "failed": sum(r.state == "failed" for r in records),
        "capped": sum(r.state == "capped" for r in records),
        "skipped_not_entitled": sum(r.skipped_not_entitled for r in records),
        "invalid_outputs": sum(r.invalid_outputs for r in records),
        "calls_per_answer": 1.0,  # caller `none`: the question is the single find
    }
    tokens = [float(r.tokens) for r in done]
    if tokens:
        out["tokens.mean"] = sum(tokens) / len(tokens)
        out["tokens.p95"] = _nearest(tokens, 0.95)
    times = [float(r.find_ms) for r in done if r.find_ms is not None]
    if times:
        out["find_ms.p50"] = _nearest(times, 0.5)
        out["find_ms.p95"] = _nearest(times, 0.95)
    gated = [r for r in done if r.gate == "not_separated"]
    if gated:
        out["judged_completion"] = sum(bool(r.judge_ran) for r in gated) / len(gated)
    for qtype in QUESTION_TYPES:
        out[f"records.{qtype}"] = sum(r.question_type == qtype for r in records)
    for slot, values in scores.items():
        name = slot.removeprefix("judge.eval.")
        if values:
            out[f"mean.{name}"] = sum(v for _, v in values) / len(values)
        if slot == "judge.eval.sufficiency":
            for qtype in QUESTION_TYPES:
                typed = [v for t, v in values if t == qtype]
                if typed:
                    out[f"mean.sufficiency.{qtype}"] = sum(typed) / len(typed)
    return out


def snapshot(
    *, eval_id: str, state: str, start_ms: int, end_ms: int, params: Mapping[str, str],
    metrics: Mapping[str, float | int], snapshot_at: str,
) -> Any:
    from reader.obs.runs import Snapshot

    return Snapshot(
        kind="eval", key=eval_id, name=f"eval {eval_id[:8]}", start_ms=start_ms, end_ms=end_ms,
        status="FINISHED" if state == "done" else "FAILED", outcome=state, end=1,
        tags={"reader.eval_id": eval_id}, params=dict(params), metrics=dict(metrics), snapshot_at=snapshot_at,
    )


RUN_ROW_SQL: Final = "SELECT * FROM obs_eval_runs WHERE id = :e"
FAIL_SQL: Final = "UPDATE obs_eval_records SET state = 'failed' WHERE eval_id = :e AND state = 'queued'"
CLOSE_SQL: Final = "UPDATE obs_eval_runs SET state = :s, finished_at = now() WHERE id = :e"
RECORDS_SQL: Final = (
    "SELECT record_id, find_ms, tokens, judge_ran, gate, state, skipped_not_entitled, invalid_outputs"
    " FROM obs_eval_records WHERE eval_id = :e"
)
SCORES_SQL: Final = "SELECT record_id, slot, judge, value FROM obs_eval_scores WHERE eval_id = :e"


def _qtype(doc: Any, record_id: str) -> str | None:
    for q in (doc or {}).get("questions") or []:
        if q.get("id") == record_id:
            return str(q.get("question_type")) if q.get("question_type") else None
    return None


def client_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(trust_env=False)


async def _close(engine: Any, args: RunArgs) -> tuple[Any, list[RecordRow], list[Any]] | None:
    """In its own committed transaction (so an MLflow failure afterwards never reopens it): fail the records
    still queued and close the run, or (before the deadline) re-enqueue the check. Returns the closed run, its
    records and scores, or None when there is nothing to write yet."""
    async with engine.begin() as own:
        await own.execute(text(LOCK_SQL), {"k": _lock_key(args.eval_id)})
        run = (await own.execute(text(RUN_ROW_SQL), {"e": args.eval_id})).first()
        if run is None:
            return None
        if run.finished_at is None:
            left = int((await own.execute(text(LEFT_SQL), {"e": args.eval_id})).scalar_one())
            now = datetime.now(UTC)
            if left and args.check >= 1 and now < run.deadline:
                await _again(args, (run.deadline - now).total_seconds())
                return None
            if left and args.check == 0:
                return None  # not every record has ended; the deadline check closes the run
            await own.execute(text(FAIL_SQL), {"e": args.eval_id})
            rows = (await own.execute(text(RECORDS_SQL), {"e": args.eval_id})).all()
            failed = all(r.state == "failed" for r in rows)
            state = "capped" if run.state == "capped" else ("failed" if failed else "done")
            await own.execute(text(CLOSE_SQL), {"s": state, "e": args.eval_id})
            run = (await own.execute(text(RUN_ROW_SQL), {"e": args.eval_id})).first()
        rows = (await own.execute(text(RECORDS_SQL), {"e": args.eval_id})).all()
        records = [RecordRow(_qtype(run.set_doc, r.record_id), r.find_ms, r.tokens, r.judge_ran, r.gate,
                             r.state, r.skipped_not_entitled, r.invalid_outputs) for r in rows]
        scores = list((await own.execute(text(SCORES_SQL), {"e": args.eval_id})).all())
    return run, records, scores


async def handle_run(args: RunArgs, conn: Any, ctx: Any) -> None:
    """Close first (own transaction), then, with tracing on, the Run and the dataset input. A retry after an
    MLflow failure finds the run closed: an incomplete Run is written again (runs.sync is idempotent); a
    complete Run without the set's dataset input gets only the input; a complete Run with it (or a run with no
    set) gets nothing (FR-OBS-39 c)."""
    closed = await _close(conn.engine, args)
    if closed is None or not tracing_on():
        return
    run, records, rows = closed
    state = str(run.state)
    now = run.finished_at or datetime.now(UTC)
    scores: dict[str, list[tuple[str | None, float]]] = {}
    judge_of: dict[str, str] = {}
    for s in rows:
        scores.setdefault(s.slot, []).append((_qtype(run.set_doc, s.record_id), float(s.value)))
        judge_of[s.slot] = s.judge
    params = {"budget_profile": run.budget_profile, "caller": run.caller}
    if run.set_sha256:
        params["set_sha256"] = run.set_sha256.strip()
    for key, value in (("brain_version", run.brain_version), ("policy_version", run.policy_version),
                       ("version_key", run.version_key)):
        if value:
            params[key] = str(value).strip()
    for slot, judge in judge_of.items():
        params[f"judge.{slot.removeprefix('judge.eval.')}"] = judge
    snap = snapshot(
        eval_id=args.eval_id, state=state, start_ms=int(run.created_at.timestamp() * 1000),
        end_ms=int(now.timestamp() * 1000), params=params, metrics=metrics(records, scores),
        snapshot_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    from reader.obs import runs

    try:
        async with client_factory() as client:
            found = await runs._find(client, snap)
            tags = runs._kv(((found or {}).get("data") or {}).get("tags"))
            with_set = bool(run.set_name and run.set_doc)
            if found is not None and tags.get("reader.complete") == "true":
                if with_set and not _has_input(found, str(run.set_name)):
                    await _log_dataset(client, str(found["info"]["run_id"]), run)
                return  # complete (with its input): a later check or retry writes nothing more
            run_id = await runs.sync(snap, client)
            if with_set:
                await _log_dataset(client, run_id, run)
    except MlflowRefused as refused:
        raise runs.ObsRunsRefused(str(refused.status)) from None


def _has_input(found: Mapping[str, Any], set_name: str) -> bool:
    """Whether the runs/search result lists the set's dataset among the Run's `inputs.dataset_inputs`."""
    listed = ((found.get("inputs") or {}).get("dataset_inputs")) or []
    names = [((d or {}).get("dataset") or {}).get("name") for d in listed if isinstance(d, dict)]
    return set_name in names


async def _again(args: RunArgs, seconds: float) -> None:
    from reader.jobs import running

    try:
        await running().enqueue(KIND_RUN, {"eval_id": args.eval_id, "check": args.check + 1},
                                schedule_in=max(1, int(seconds)))
    except Exception:
        log.warning("obs eval_run_requeue_failed")


async def _log_dataset(client: httpx.AsyncClient, run_id: str, run: Any) -> None:
    """The pinned set as a dataset in `bench` (question text, ids and gold pages only; never expected facts),
    logged as the Run's input (FR-OBS-23 d's shape)."""
    from reader.obs.bench import _bench_experiment

    experiment = await _bench_experiment(client)
    dataset_id = await _dataset(client, run, [experiment])
    try:
        await _log_input(client, run_id, run, dataset_id)
    except MlflowRefused as refused:
        if refused.status != 400:
            raise
        # FR-OBS-39 (c)'s declared fallback: MLflow refused a `bench` dataset as an input of a Run in
        # experiment 0, so the dataset found by name in experiment 0 is used, else one is made in both
        # experiments; found first, so a second execution reuses it instead of making another.
        dataset_id = await _dataset(client, run, ["0", experiment], search_in=["0"])
        await _log_input(client, run_id, run, dataset_id)


def dataset_records(set_doc: Any) -> list[dict[str, Any]]:
    """VEC-OBS-5 `eval_record`: question text, ids, the question type and, where the set gives them, chapter
    and gold pages (never invented); expected facts never."""
    out: list[dict[str, Any]] = []
    for q in (set_doc or {}).get("questions") or []:
        inputs: dict[str, Any] = {"question_id": str(q["id"]), "question": str(q.get("question", "")),
                                  "question_type": str(q.get("question_type", ""))}
        if isinstance(q.get("chapter"), int):
            inputs["chapter"] = q["chapter"]
        pages = [p for p in q.get("gold_pages") or [] if isinstance(p, int)]
        out.append({"inputs": inputs, "expectations": {"gold_pages": pages}} if pages else {"inputs": inputs})
    return out


async def _dataset(
    client: httpx.AsyncClient, run: Any, experiments: list[str], search_in: list[str] | None = None
) -> str:
    """The dataset named after the set, found in `search_in` (default `experiments`), else created in
    `experiments`; its records upserted."""
    _, found = await request(client, "POST", "/api/3.0/mlflow/datasets/search",
                             {"experiment_ids": search_in or experiments,
                              "filter_string": f"name = '{run.set_name}'"})
    listed = [d for d in found.get("datasets") or [] if isinstance(d, dict)]
    if listed:
        dataset = listed[0]
    else:
        _, made = await request(client, "POST", "/api/3.0/mlflow/datasets/create", {
            "name": run.set_name, "experiment_ids": experiments,
            "tags": json.dumps({"reader.source_sha256": str(run.set_sha256).strip(), "reader.eval": "brain"}),
        })
        dataset = made.get("dataset") or {}
    dataset_id = str(dataset["dataset_id"])
    await request(client, "POST", f"/api/3.0/mlflow/datasets/{dataset_id}/records",
                  {"records": json.dumps(dataset_records(run.set_doc))})
    return dataset_id


async def _log_input(client: httpx.AsyncClient, run_id: str, run: Any, dataset_id: str) -> None:
    await request(client, "POST", "/api/2.0/mlflow/runs/log-inputs", {
        "run_id": run_id,
        "datasets": [{"dataset": {"name": run.set_name, "digest": str(run.set_sha256).strip()[:8],
                                  "source_type": "mlflow_evaluation_dataset",
                                  "source": json.dumps({"dataset_id": dataset_id})},
                      "tags": [{"key": "mlflow.data.context", "value": "evaluation"}]}],
    })


# --- the score post ---


class ScoreArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    score_id: str = Field(pattern=UUID_RE)


SCORE_ROW_SQL: Final = (
    "SELECT id, slot, judge, value, trace_id, assessment_id FROM obs_eval_scores WHERE id = :i"
)
SAVE_SQL: Final = "UPDATE obs_eval_scores SET assessment_id = :a WHERE id = :i"


async def post_score(conn: Any, score_id: str, client: httpx.AsyncClient, last: bool = False) -> None:
    """FR-OBS-14 as amended: one LLM_JUDGE assessment per score, found again by name, source_id and
    score_id."""
    from reader.obs import feedback

    row = (await conn.execute(text(SCORE_ROW_SQL), {"i": score_id})).first()
    if row is None or row.trace_id is None:
        return
    name = "eval." + str(row.slot).removeprefix("judge.eval.")
    ref = f"tr-{str(row.trace_id).strip()}"
    status, got = await request(client, "GET", "/ajax-api/3.0/mlflow/traces/get", params={"trace_id": ref},
                                ok404=True)
    if status == 404:
        if not last:
            raise TraceMissing(name)
        log.info("obs judge_score no_trace name=%s", name)
        return
    info = (got.get("trace") or {}).get("trace_info") or {}
    for there in info.get("assessments") or []:
        source = there.get("source") or {}
        meta = there.get("metadata") or {}
        if (there.get("assessment_name") == name and source.get("source_id") == row.judge
                and str(meta.get("score_id")) == str(row.id)):
            await conn.execute(text(SAVE_SQL), {"a": str(there.get("assessment_id")), "i": row.id})
            return
    body = feedback.eval_body(name, float(row.value), str(row.id), str(row.judge), ref)
    status, posted = await request(client, "POST", f"/api/3.0/mlflow/traces/{ref}/assessments", body,
                                   ok404=True)
    if status == 404:
        if not last:
            raise TraceMissing(name)
        return
    await conn.execute(text(SAVE_SQL), {"a": str((posted.get("assessment") or {})["assessment_id"]),
                                        "i": row.id})


async def handle_score(args: ScoreArgs, conn: Any, ctx: Any) -> None:
    if not tracing_on():
        return
    last = int(getattr(ctx, "attempts", 0)) + 1 >= 3
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            await post_score(conn, args.score_id, client, last=last)
    except MlflowRefused as refused:
        raise ScoreRefused(str(refused.status)) from None


# --- the sample start ---


class SampleArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slot: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}$")


SAMPLE_SQL: Final = (
    "SELECT ji.id FROM judge_inputs ji WHERE ji.labelable AND ji.created_at > now() - interval '1 day'"
    " AND NOT EXISTS (SELECT 1 FROM obs_eval_scores s WHERE s.judge_input_id = ji.id)"
    " AND NOT EXISTS (SELECT 1 FROM obs_eval_records e WHERE e.judge_input_id = ji.id)"
    " ORDER BY ji.created_at DESC LIMIT :n"
)
OWNER_SQL: Final = "SELECT id FROM users WHERE role = 'owner' ORDER BY created_at LIMIT 1"


@dataclass
class SamplePolicy:
    """The policy's sample settings: judges and the eval cap; off (no judges) by default until judge-gate."""

    judges: tuple[str, ...] = ()
    cap_usd: Decimal | None = None
    floors: Mapping[str, Decimal] = field(default_factory=dict)


SAMPLE_POLICY: SamplePolicy = SamplePolicy()


async def handle_sample(args: SampleArgs, conn: Any, ctx: Any) -> None:
    if not SAMPLE_POLICY.judges:
        return
    owner = (await conn.execute(text(OWNER_SQL))).scalar()
    ids = [r.id for r in (await conn.execute(text(SAMPLE_SQL), {"n": SAMPLE_MAX})).all()]
    if owner is None or not ids:
        return
    try:
        await start(conn, owner, "mcp", SAMPLE_POLICY.judges, sample_ids=ids, cap_usd=SAMPLE_POLICY.cap_usd,
                    floors=SAMPLE_POLICY.floors)
    except StartRefused as refused:
        log.info("obs eval_sample_refused code=%s", refused.code)


KIND_DECL: Final = JobKind(name=KIND, queue="obs", args=EvalArgs, key=("eval_id", "record_no"),
                           handler=handle_record, max_attempts=1, timeout_s=RECORD_S)
RUN_DECL: Final = JobKind(name=KIND_RUN, queue="obs", args=RunArgs, key=("eval_id", "check"),
                          handler=handle_run, max_attempts=3, transient=(httpx.TransportError,), timeout_s=60)
SCORE_DECL: Final = JobKind(name=KIND_SCORE, queue="obs", args=ScoreArgs, key=("score_id",),
                            handler=handle_score, max_attempts=3,
                            transient=(TraceMissing, httpx.TransportError), timeout_s=30)
SAMPLE_DECL: Final = JobKind(name=KIND_SAMPLE, queue="obs", args=SampleArgs, key=("slot",),
                             handler=handle_sample, max_attempts=1, timeout_s=30)
JOB_KINDS: Final = (KIND_DECL, RUN_DECL, SCORE_DECL, SAMPLE_DECL)
Mode = Literal["dataset", "sample"]
