"""SRV-OBS FR-OBS-41: alignment of a judge slot's instructions section from the owner's labels, job kind
`obs.align` {proposal_id, step}; owner decision 14: no model weights.

A GEPA-style greedy instruction search split into re-enqueued steps (each at most one `judge.align` call or
one batch of sequential judge re-runs), state in the `judge_prompt_proposals` row. Candidates enter a judge
only through `prompts.wrap`; nothing is recorded to, or looked up in, SRV-AGENT's `judge_outputs` or the lane.
Candidate text lives only in the row's `candidates_under_test` / `candidate_text` while running and in
`proposed_text` after; never in job args, logs or traces.

The pure rules (agreement, split, counting, round results, refusal orders, end-code precedence) are module
functions; the job takes its dependencies through `Deps` (ports to SRV-AGENT's Provider, SRV-SEARCH's card
reader and policy, SRV-INGEST's material reader). Production wiring: `configure(Deps(...))`."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from reader.jobs.model import JobKind
from reader.obs import align_guard, judges, phase2, prompts

VEC: Final[dict[str, Any]] = phase2.VEC["align"]
LIMITS: Final[dict[str, Any]] = VEC["limits"]
KIND: Final = "obs.align"
LINE: Final = "eval"
ALIGN_SLOT: Final = "judge.align"
FIND_SLOT: Final = "judge.find"
EVAL_SLOTS: Final = tuple(s for s in prompts.SLOT_FILES if s != ALIGN_SLOT)
EVERY: Final[int] = int(LIMITS["every"])
TRAIN: Final[int] = int(LIMITS["train"])
HELDOUT: Final[int] = int(LIMITS["heldout"])
MIN_SPLIT: Final[int] = int(LIMITS["min_split"])
ROUNDS: Final[int] = int(LIMITS["rounds"])
RERUN_DEADLINE_S: Final[float] = float(VEC["rerun_deadline_s"])
SCORING_BUDGET_S: Final[float] = float(VEC["scoring_step_budget_s"])
MAX_MISSING_SHARE: Final[float] = float(VEC["max_missing_share"])
PROPOSE_DEADLINE_S: Final = 150.0  # a judge.align call, inside the 180 s step timeout (not set by the spec)
MAX_STEP: Final = 100
DEFAULT_MODEL: Final = "opus"  # SRV-AGENT's v1 binding of the judge.eval.* slots
EMPTY_SHA: Final = hashlib.sha256(b"").hexdigest()
UUID_RE: Final = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


# --- the pure rules ---


@dataclass(frozen=True)
class FindOutput:
    """A judge.find output for agreement: the kept card ids (order ignored) and the sufficiency."""

    keep: frozenset[str]
    sufficiency: float | None


def eq_find(o: FindOutput, r: FindOutput, threshold: float) -> bool:
    """The same kept card set and the same side of the sufficiency threshold (or both absent)."""

    def side(s: float | None) -> bool | None:
        return None if s is None else s >= threshold

    return o.keep == r.keep and side(o.sufficiency) == side(r.sufficiency)


def eq_eval(o: float, r: float, threshold: float) -> bool:
    """The same side of the slot's pass threshold."""
    return (o >= threshold) == (r >= threshold)


def outcome(ok: bool, same: bool | None) -> str:
    """ok true agrees iff eq; ok false agrees iff not eq; an unparsable re-run (None) disagrees."""
    if same is None:
        return "disagree"
    return "agree" if same == ok else "disagree"


def threshold_key(slot: str) -> str:
    return f"{FIND_SLOT}.sufficiency_threshold" if slot == FIND_SLOT else f"{slot}.pass_threshold"


def _rng(*parts: object) -> random.Random:
    seed = hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()
    return random.Random(seed)  # noqa: S311 (a reproducible split, not a secret)


def split(label_ids: Sequence[str], slot: str, at_labels: int) -> tuple[list[str], list[str]]:
    """(training, held-out) from the newest TRAIN + HELDOUT labels (input newest first), seeded by (slot,
    at_labels), in the TRAIN:HELDOUT proportion."""
    order = list(label_ids[: TRAIN + HELDOUT])
    _rng(slot, at_labels).shuffle(order)
    n_held = min(HELDOUT, len(order) * HELDOUT // (TRAIN + HELDOUT))
    return order[n_held : n_held + TRAIN], order[:n_held]


def halves(errors: Sequence[str], slot: str, at_labels: int, round_no: int) -> tuple[list[str], list[str]]:
    """The two seeded halves of the training errors for one round's two proposing steps."""
    order = list(errors)
    _rng(slot, at_labels, "round", round_no).shuffle(order)
    return order[0::2], order[1::2]


def missing_share(outcomes: Mapping[str, str], n_heldout: int) -> float:
    missing = sum(1 for v in outcomes.values() if v == "missing") + max(n_heldout - len(outcomes), 0)
    return missing / n_heldout if n_heldout else 1.0


def rates(best: Mapping[str, str], other: Mapping[str, str]) -> tuple[float | None, float | None, int]:
    """Agreement rates of both over the labels counted (not missing) for both, and their number."""
    common = [k for k, v in best.items() if v != "missing" and other.get(k, "missing") != "missing"]
    if not common:
        return None, None, 0
    n = len(common)
    return (sum(best[k] == "agree" for k in common) / n, sum(other[k] == "agree" for k in common) / n, n)


@dataclass(frozen=True)
class RoundResult:
    promoted: int | None
    scored: int
    dropped_missing: int


def round_result(best: Mapping[str, str], cands: Sequence[Mapping[str, str]], n_heldout: int) -> RoundResult:
    """A candidate over the missing share is dropped; the one with the highest rate becomes the current best
    only if it beats the current best's rate (a tie keeps the current best)."""
    promoted: int | None = None
    top = -1.0
    scored = dropped = 0
    for i, cand in enumerate(cands):
        if missing_share(cand, n_heldout) > MAX_MISSING_SHARE:
            dropped += 1
            continue
        scored += 1
        best_rate, cand_rate, _ = rates(best, cand)
        if best_rate is not None and cand_rate is not None and cand_rate > best_rate and cand_rate > top:
            promoted, top = i, cand_rate
    return RoundResult(promoted, scored, dropped)


def next_round(round_no: int, *, improved: bool) -> int | None:
    """The next round, or None: the run ends after the first round without improvement or after ROUNDS."""
    return round_no + 1 if improved and round_no < ROUNDS else None


def end_code(*, scored_any: bool, dropped_missing: int, dropped_guard: int) -> str:
    """The refusal when the current best is still the base."""
    if scored_any:
        return "no_change"
    if dropped_missing and not dropped_guard:
        return "provider_error"
    if dropped_guard and not dropped_missing:
        return "all_guard_refused"
    return "provider_error"


def step0_refusal(*, frame_ok: bool, base_ok: bool, slot_supported: bool, threshold_present: bool,
                  n_train: int, n_heldout: int, n_errors: int) -> str | None:
    checks = (
        (frame_ok, "frame_changed"),
        (base_ok, "base_changed"),
        (slot_supported, "slot_unsupported"),
        (threshold_present, "threshold_missing"),
        (n_train >= MIN_SPLIT and n_heldout >= MIN_SPLIT, "too_few_labels"),
        (n_errors > 0, "no_training_errors"),
    )
    return next((code for ok, code in checks if not ok), None)


def later_refusal(*, frame_ok: bool, base_ok: bool, policy_same: bool) -> str | None:
    checks = ((frame_ok, "frame_changed"), (base_ok, "base_changed"), (policy_same, "policy_changed"))
    return next((code for ok, code in checks if not ok), None)


# --- the ports ---


class NotWired(Exception):
    """A production dependency that is not on this branch yet (paired change)."""


class CapReached(Exception):
    """Raised by the Provider port when `budgets.eval_usd_per_run` is reached for the run."""


class Provider(Protocol):
    """SRV-AGENT's Provider port on line `eval`, one tool-less call; the reply text. Raises CapReached at the
    eval cap, any other exception for a provider failure."""

    async def __call__(self, *, slot: str, system: str, user: str, line: str, run_id: uuid.UUID,
                       deadline_s: float, thinking_off: bool, abort_on_retry: bool) -> str: ...


class CardReader(Protocol):
    """SRV-SEARCH's card text by id under B8 for the payer: only entitled, resolvable cards are returned."""

    async def __call__(
        self, conn: Any, owner_id: uuid.UUID, card_ids: Sequence[str]
    ) -> Mapping[str, str]: ...


class MaterialReader(Protocol):
    """SRV-INGEST's text layer of the materials the cards come from (for the guard), in memory."""

    async def __call__(self, conn: Any, card_ids: Sequence[str]) -> Sequence[str]: ...


class PolicyReader(Protocol):
    """SRV-SEARCH's loaded policy: (policy_version, thresholds by policy key)."""

    async def __call__(self, conn: Any) -> tuple[str, Mapping[str, float]]: ...


FindParser = Callable[[str, Sequence[str]], FindOutput]


async def _provider_not_wired(**_: Any) -> str:
    raise NotWired("provider")


async def _cards_not_wired(conn: Any, owner_id: uuid.UUID, card_ids: Sequence[str]) -> Mapping[str, str]:
    raise NotWired("cards")


async def _material_not_wired(conn: Any, card_ids: Sequence[str]) -> Sequence[str]:
    raise NotWired("material")


async def _policy_not_wired(conn: Any) -> tuple[str, Mapping[str, float]]:
    raise NotWired("policy")


def _find_parser_not_wired(raw: str, card_ids: Sequence[str]) -> FindOutput:
    raise NotWired("judge.find parser")


def _default_judge(slot: str) -> str:
    return judges.judge_name(slot, DEFAULT_MODEL)


@dataclass(frozen=True)
class Deps:
    provider: Provider = field(default=_provider_not_wired)
    cards: CardReader = field(default=_cards_not_wired)
    material: MaterialReader = field(default=_material_not_wired)
    policy: PolicyReader = field(default=_policy_not_wired)
    current_judge: Callable[[str], str] = _default_judge
    judge_find_supported: bool = False  # SRV-AGENT's capability flag (paired change)
    parse_find: FindParser = _find_parser_not_wired
    clock: Callable[[], float] = time.monotonic


_DEPS: list[Deps] = []


def configure(deps: Deps) -> None:
    _DEPS[:] = [deps]


# --- labels ---


@dataclass(frozen=True)
class Label:
    label_id: str
    ok: bool
    note: str | None
    query: str
    card_ids: tuple[str, ...]
    facts: tuple[str, ...]
    recorded: float | FindOutput
    judge: str


EVAL_LABELS_SQL: Final = (
    "SELECT l.id::text AS label_id, l.ok, l.note, s.value, s.judge, s.judge_input_id, s.record_id,"
    " ji.query AS ji_query, ji.card_ids AS ji_cards, ji.labelable, r.card_ids AS rec_cards, er.set_doc"
    " FROM owner_labels l JOIN obs_eval_scores s ON s.id = l.target_id AND s.owner_id = l.owner_id"
    " AND s.slot = l.slot"
    " LEFT JOIN judge_inputs ji ON ji.id = s.judge_input_id AND ji.owner_id = l.owner_id"
    " LEFT JOIN obs_eval_records r ON r.eval_id = s.eval_id AND r.record_id = s.record_id"
    " LEFT JOIN obs_eval_runs er ON er.id = s.eval_id AND er.owner_id = l.owner_id"
    " WHERE l.owner_id = :o AND l.target = 'judge' AND l.slot = :s ORDER BY l.created_at DESC, l.id DESC"
)
FIND_LABELS_SQL: Final = (
    "SELECT l.id::text AS label_id, l.ok, l.note, ji.query AS ji_query, ji.card_ids AS ji_cards, ji.output,"
    " ji.judge, ji.labelable FROM owner_labels l JOIN judge_inputs ji ON ji.id = l.target_id"
    " AND ji.owner_id = l.owner_id"
    " WHERE l.owner_id = :o AND l.target = 'judge' AND l.slot = :s ORDER BY l.created_at DESC, l.id DESC"
)


def _record(doc: Any, record_id: str) -> Mapping[str, Any] | None:
    """A dataset record of the pinned set (`questions` or `records`, keyed by `id` or `question_id`)."""
    if not isinstance(doc, dict):
        return None
    for item in doc.get("questions") or doc.get("records") or []:
        if isinstance(item, dict) and record_id in (item.get("id"), item.get("question_id")):
            return item
    return None


def _label(slot: str, row: Any) -> Label | None:
    """The label with its input, or None when the input expired or is not labelable."""
    if slot == FIND_SLOT:
        if not row.labelable or row.ji_query is None:
            return None
        cards = tuple(row.ji_cards)
        out = row.output or {}
        keep = frozenset(cards[o - 1] for o in out.get("keep") or [] if 1 <= o <= len(cards))
        recorded: float | FindOutput = FindOutput(keep, out.get("sufficiency"))
        return Label(row.label_id, row.ok, row.note, row.ji_query, cards, (), recorded, row.judge)
    if row.judge_input_id is not None:
        if not row.labelable or row.ji_query is None:
            return None
        return Label(row.label_id, row.ok, row.note, row.ji_query, tuple(row.ji_cards), (), float(row.value),
                     row.judge)
    item = _record(row.set_doc, row.record_id or "")
    if row.rec_cards is None or item is None or not isinstance(item.get("question"), str):
        return None
    facts = tuple(str(f) for f in item.get("expected_facts") or [])
    return Label(row.label_id, row.ok, row.note, item["question"], tuple(row.rec_cards), facts,
                 float(row.value), row.judge)


async def load_labels(conn: Any, owner_id: uuid.UUID, slot: str) -> list[Label]:
    """The slot's judge labels whose inputs still exist and are labelable, newest first."""
    sql = FIND_LABELS_SQL if slot == FIND_SLOT else EVAL_LABELS_SQL
    rows = (await conn.execute(text(sql), {"o": owner_id, "s": slot})).all()
    return [label for row in rows if (label := _label(slot, row)) is not None]


async def label_count(conn: Any, owner_id: uuid.UUID, slot: str) -> int:
    """The slot's judge label rows (a relabel updates its row, so it does not count)."""
    found = await conn.execute(
        text("SELECT count(*) FROM owner_labels WHERE owner_id = :o AND target = 'judge' AND slot = :s"),
        {"o": owner_id, "s": slot},
    )
    return int(found.scalar_one())


async def guard_inputs(conn: Any, deps: Deps, owner_id: uuid.UUID, slot: str, label_ids: Sequence[str], *,
                       strict: bool) -> align_guard.GuardInputs | None:
    """The guard's inputs for the labels used: queries (whole), card texts, expected facts and material text
    (runs). With `strict`, None when any label's input or card no longer resolves (FR-OBS-42)."""
    wanted = {str(i) for i in label_ids}
    labels = [lb for lb in await load_labels(conn, owner_id, slot) if lb.label_id in wanted]
    if strict and len(labels) < len(wanted):
        return None
    queries: list[str] = []
    texts: list[str] = []
    card_ids: list[str] = []
    for label in labels:
        found = await deps.cards(conn, owner_id, label.card_ids)
        if strict and any(c not in found for c in label.card_ids):
            return None
        queries.append(label.query)
        texts += [found[c] for c in label.card_ids if c in found] + list(label.facts)
        card_ids += [c for c in label.card_ids if c in found]
    material = await deps.material(conn, card_ids)
    return align_guard.GuardInputs(_base_text(slot), tuple(queries), (*texts, *material))


# --- the file state ---


def _base_text(slot: str) -> str:
    try:
        return prompts.section(slot)
    except (LookupError, OSError):
        return ""


def file_state(slot: str) -> tuple[str, str]:
    """(frame_sha256, sha256 of the section) of the slot's current file; SRV-AGENT's judge.find file before
    its paired change has neither, which reads as the sha256 of nothing for both."""
    try:
        return prompts.frame_sha256(slot), prompts.section_sha256(slot)
    except (LookupError, OSError):
        return EMPTY_SHA, EMPTY_SHA


# --- the trigger ---


class AlignArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_id: str = Field(pattern=UUID_RE)
    step: int = Field(ge=0, le=MAX_STEP)


INSERT_SQL: Final = (
    "INSERT INTO judge_prompt_proposals (id, owner_id, slot, at_labels, step, base_prompt_sha256,"
    " frame_sha256, state, guard_version) VALUES (:id, :o, :s, :n, 0, :b, :f, 'running', :g)"
    " ON CONFLICT (owner_id, slot, at_labels) DO NOTHING RETURNING id"
)


async def start_if_due(conn: Any, owner_id: uuid.UUID, slot: str, label_count: int) -> uuid.UUID | None:
    """In the label insert's transaction: when the slot's label count is a positive multiple of EVERY, the
    running row and the outbox key of `obs.align` {proposal_id, step 0}; None otherwise or when that count
    already started one."""
    if label_count <= 0 or label_count % EVERY or slot not in (*EVAL_SLOTS, FIND_SLOT):
        return None
    frame, base = file_state(slot)
    found = (await conn.execute(text(INSERT_SQL), {"id": uuid.uuid4(), "o": owner_id, "s": slot,
                                                   "n": label_count, "b": base, "f": frame,
                                                   "g": align_guard.GUARD_VERSION})).first()
    if found is None:
        return None
    pid = uuid.UUID(str(found.id))
    await phase2.stage(conn, KIND_DECL, {"proposal_id": str(pid), "step": 0})
    return pid


async def mark_lost(conn: Any) -> int:
    """For `obs.retention` (FR-OBS-11 as edited): a row still running a day after its last step becomes
    refused `step_lost`, its candidate columns nulled; returns the number of rows."""
    done = await conn.execute(text(
        "UPDATE judge_prompt_proposals SET state = 'refused', refusal = 'step_lost', candidate_text = NULL,"
        " candidates_under_test = NULL, updated_at = now()"
        " WHERE state = 'running' AND updated_at < now() - interval '1 day'"
    ))
    return int(done.rowcount)


# --- the step machine ---


@dataclass
class _Run:
    pid: uuid.UUID
    owner_id: uuid.UUID
    slot: str
    at_labels: int
    step: int
    policy_version: str | None
    work: dict[str, Any]
    scores: dict[str, Any]
    candidate_text: str | None
    cut: list[str]
    label_ids: list[str]


SAVE_SQL: Final = (
    "UPDATE judge_prompt_proposals SET step = :step, state = :state, refusal = :refusal,"
    " work = CAST(:work AS jsonb), scores = CAST(:scores AS jsonb), candidate_text = :ct,"
    " candidates_under_test = :cut, policy_version = :pv, proposed_text = :pt, proposed_sha256 = :ps,"
    " label_ids = CAST(:labels AS uuid[]), updated_at = now()"
    " WHERE id = :id"
)


async def _save(conn: Any, run: _Run, state: str = "running", refusal: str | None = None,
                proposed: str | None = None) -> None:
    running = state == "running"
    await conn.execute(text(SAVE_SQL), {
        "id": run.pid, "step": run.step, "state": state, "refusal": refusal, "work": json.dumps(run.work),
        "scores": json.dumps(run.scores), "ct": run.candidate_text if running else None,
        "cut": (run.cut or None) if running else None, "pv": run.policy_version, "pt": proposed,
        "ps": hashlib.sha256(proposed.encode()).hexdigest() if proposed is not None else None,
        "labels": run.label_ids,
    })


async def _advance(conn: Any, run: _Run) -> None:
    if run.step + 1 > MAX_STEP:
        await _refuse(conn, run, "step_lost")
        return
    run.step += 1
    await _save(conn, run)
    await phase2.stage(conn, KIND_DECL, {"proposal_id": str(run.pid), "step": run.step})


def _final_rates(run: _Run) -> None:
    base: Mapping[str, str] = run.work.get("base") or {}
    best: Mapping[str, str] | None = run.work.get("best")
    if best is not None and run.candidate_text is not None:
        base_rate, proposed_rate, n = rates(base, best)
        missing = {k for k in {*base, *best} if "missing" in (base.get(k), best.get(k))}
    else:
        counted = [v for v in base.values() if v != "missing"]
        base_rate = sum(v == "agree" for v in counted) / len(counted) if counted else None
        proposed_rate, n = None, len(counted)
        missing = {k for k, v in base.items() if v == "missing"}
    run.scores.update(base_rate=base_rate, proposed_rate=proposed_rate, n_compared=n, n_missing=len(missing))


async def _refuse(conn: Any, run: _Run, code: str) -> None:
    _final_rates(run)
    await _save(conn, run, "refused", code)


def _count(run: _Run, key: str, by: int = 1) -> None:
    run.scores[key] = int(run.scores.get(key, 0)) + by


async def _finish(conn: Any, run: _Run, deps: Deps) -> None:
    """The end of the last round: proposed after the final-text guard, else the end-code precedence."""
    _final_rates(run)
    if run.candidate_text is None:
        code = end_code(scored_any=bool(run.work.get("scored_any")), dropped_missing=int(run.scores.get(
            "dropped_missing", 0)), dropped_guard=int(run.scores.get("dropped_guard", 0)))
        await _save(conn, run, "refused", code)
        return
    inputs = await guard_inputs(conn, deps, run.owner_id, run.slot, run.label_ids, strict=False)
    assert inputs is not None  # noqa: S101 (never None without strict)
    verdict = align_guard.guard(run.candidate_text, inputs)  # the second run: no guard.<code> count
    if verdict is not None:
        await _save(conn, run, "refused", verdict)
        return
    await _save(conn, run, "proposed", proposed=run.candidate_text)


async def _load(conn: Any, pid: str) -> tuple[_Run, str] | None:
    row = (await conn.execute(text("SELECT * FROM judge_prompt_proposals WHERE id = :id FOR UPDATE"),
                              {"id": pid})).first()
    if row is None or row.state != "running":
        return None
    run = _Run(uuid.UUID(str(row.id)), row.owner_id, row.slot, int(row.at_labels), int(row.step),
               row.policy_version, dict(row.work or {}), dict(row.scores or {}), row.candidate_text,
               list(row.candidates_under_test or []), [str(i) for i in row.label_ids or []])
    return run, f"{row.frame_sha256}:{row.base_prompt_sha256}"


async def run_step(conn: Any, args: AlignArgs, deps: Deps) -> None:
    """One step, in the job's transaction. A stale step (the row moved on or ended) does nothing."""
    loaded = await _load(conn, args.proposal_id)
    if loaded is None or loaded[0].step != args.step:
        return
    run, pinned = loaded
    frame, base = file_state(run.slot)
    pinned_frame, pinned_base = pinned.split(":")
    frame_ok, base_ok = pinned_frame == frame, pinned_base == base
    version, thresholds = await deps.policy(conn)
    try:
        if run.step == 0:
            await _step0(conn, run, deps, frame_ok, base_ok, version, thresholds)
            return
        refusal = later_refusal(frame_ok=frame_ok, base_ok=base_ok, policy_same=version == run.policy_version)
        if refusal is not None:
            await _refuse(conn, run, refusal)
            return
        threshold = float(thresholds[threshold_key(run.slot)])
        if run.work.get("phase") == "propose":
            await _propose(conn, run, deps)
        else:
            await _score(conn, run, deps, threshold)
    except CapReached:
        await _refuse(conn, run, "eval_cap")


async def _step0(conn: Any, run: _Run, deps: Deps, frame_ok: bool, base_ok: bool, version: str,
                 thresholds: Mapping[str, float]) -> None:
    if frame_ok and base_ok:
        run.policy_version = version  # pinned after the frame and base checks
    supported = run.slot != FIND_SLOT or deps.judge_find_supported
    has_threshold = threshold_key(run.slot) in thresholds
    first = step0_refusal(frame_ok=frame_ok, base_ok=base_ok, slot_supported=supported,
                          threshold_present=has_threshold, n_train=MIN_SPLIT, n_heldout=MIN_SPLIT, n_errors=1)
    if first is not None:
        await _refuse(conn, run, first)
        return
    usable: list[Label] = []
    skipped = 0
    for label in await load_labels(conn, run.owner_id, run.slot):
        if len(usable) == TRAIN + HELDOUT:
            break
        found = await deps.cards(conn, run.owner_id, label.card_ids)
        if any(c not in found for c in label.card_ids):
            skipped += 1  # B8: a card the owner is not entitled to (or that no longer resolves)
            continue
        usable.append(label)
    train, held = split([lb.label_id for lb in usable], run.slot, run.at_labels)
    ok = {lb.label_id: lb.ok for lb in usable}
    n_errors = sum(1 for lid in train if not ok[lid])
    run.label_ids = train + held
    run.scores = {"n_train": len(train), "n_heldout": len(held), "n_candidates": 0, "dropped_missing": 0,
                  "dropped_guard": 0, "rounds": 0, "skipped_not_entitled": skipped}
    run.work = {"train": train, "heldout": held, "phase": "propose", "half": 0, "round": 1, "base": {},
                "cand": [], "best": None, "scored_any": False, "cands": []}
    refusal = step0_refusal(frame_ok=True, base_ok=True, slot_supported=True, threshold_present=True,
                            n_train=len(train), n_heldout=len(held), n_errors=n_errors)
    if refusal is not None:
        await _refuse(conn, run, refusal)
        return
    await _advance(conn, run)


def _recorded_text(label: Label) -> str:
    if isinstance(label.recorded, FindOutput):
        kept = [i for i, c in enumerate(label.card_ids, start=1) if c in label.recorded.keep]
        return f"kept cards {kept}, sufficiency {label.recorded.sufficiency}"
    return f"score {label.recorded:.2f}"


async def _propose(conn: Any, run: _Run, deps: Deps) -> None:
    """One proposing step: at most one judge.align call over one seeded half of the training errors."""
    work = run.work
    round_no = int(work["round"])
    run.scores["rounds"] = round_no
    labels = {lb.label_id: lb for lb in await load_labels(conn, run.owner_id, run.slot)}
    errors = [lid for lid in work["train"] if lid in labels and not labels[lid].ok]
    half = halves(errors, run.slot, run.at_labels, round_no)[int(work["half"])]
    if half:
        cases = []
        for lid in half:
            label = labels[lid]
            found = await deps.cards(conn, run.owner_id, label.card_ids)
            cases.append(judges.AlignError(label.query, [found[c] for c in label.card_ids if c in found],
                                           label.facts, _recorded_text(label), label.note))
        current = run.candidate_text if run.candidate_text is not None else prompts.section(run.slot)
        system, user = judges.align_messages(current, cases)
        try:
            raw = await deps.provider(slot=ALIGN_SLOT, system=system, user=user, line=LINE, run_id=run.pid,
                                      deadline_s=PROPOSE_DEADLINE_S, thinking_off=True, abort_on_retry=True)
        except (CapReached, NotWired):
            raise
        except Exception:
            await _refuse(conn, run, "provider_error")
            return
        try:
            candidate = judges.parse_align(raw)
        except judges.OutputInvalid:
            await _refuse(conn, run, "output_invalid")
            return
        _count(run, "n_candidates")
        inputs = await guard_inputs(conn, deps, run.owner_id, run.slot, run.label_ids, strict=False)
        assert inputs is not None  # noqa: S101
        verdict = align_guard.guard(candidate, inputs)
        if verdict is not None:
            _count(run, "dropped_guard")
            _count(run, f"guard.{verdict}")
        else:
            run.cut.append(candidate)
            work["cand"].append({})
            work["cands"].append(hashlib.sha256(candidate.encode()).hexdigest())
    if int(work["half"]) == 0:
        work["half"] = 1
        await _advance(conn, run)
        return
    if not run.cut:  # nothing to score: round 1 ends by the precedence, a later round without improvement
        await _finish(conn, run, deps)
        return
    work["phase"] = "score"
    await _advance(conn, run)


async def _rerun(conn: Any, run: _Run, deps: Deps, label: Label | None, instructions: str,
                 threshold: float) -> str:
    """One held-out label under `instructions`: agree, disagree or missing."""
    if label is None:
        return "missing"
    found = await deps.cards(conn, run.owner_id, label.card_ids)
    if any(c not in found for c in label.card_ids):
        return "missing"
    cards = [(c, found[c]) for c in label.card_ids]
    if run.slot == FIND_SLOT:
        system = prompts.wrap(run.slot, instructions)
        user = judges.render_data([("Question", label.query), ("Cards", [t for _, t in cards])])
    else:
        system, user = judges.eval_messages(run.slot, instructions, label.query, cards, label.facts)
    try:
        raw = await asyncio.wait_for(
            deps.provider(slot=run.slot, system=system, user=user, line=LINE, run_id=run.pid,
                          deadline_s=RERUN_DEADLINE_S, thinking_off=True, abort_on_retry=True),
            RERUN_DEADLINE_S,
        )
    except (CapReached, NotWired):
        raise
    except Exception:
        return "missing"  # a timeout or a provider failure
    try:
        if isinstance(label.recorded, FindOutput):
            same: bool | None = eq_find(deps.parse_find(raw, label.card_ids), label.recorded, threshold)
        else:
            same = eq_eval(judges.parse_eval(raw), label.recorded, threshold)
    except (judges.OutputInvalid, ValueError):
        same = None  # a parse failure counts as a disagreement
    return outcome(label.ok, same)


async def _score(conn: Any, run: _Run, deps: Deps, threshold: float) -> None:
    """A scoring step: sequential re-runs (round 1: the base where no current-version recorded output exists;
    then each candidate) until SCORING_BUDGET_S has elapsed, then the rest in the next step."""
    work = run.work
    start = deps.clock()
    held: list[str] = work["heldout"]
    labels = {lb.label_id: lb for lb in await load_labels(conn, run.owner_id, run.slot)}
    current = deps.current_judge(run.slot)
    tasks: list[tuple[int, str]] = []  # (-1 for the base, else the candidate index; label id)
    if int(work["round"]) == 1:
        tasks += [(-1, lid) for lid in held if lid not in work["base"]]
    for i, outcomes in enumerate(work["cand"]):
        tasks += [(i, lid) for lid in held if lid not in outcomes]
    for index, lid in tasks:
        label = labels.get(lid)
        if index < 0 and label is not None and label.judge == current:
            work["base"][lid] = outcome(label.ok, True)  # the recorded output counts without a call
            continue
        if deps.clock() - start >= SCORING_BUDGET_S:
            await _advance(conn, run)
            return
        text_ = prompts.section(run.slot) if index < 0 else run.cut[index]
        got = await _rerun(conn, run, deps, label, text_, threshold)
        (work["base"] if index < 0 else work["cand"][index])[lid] = got
    best = work["base"] if run.candidate_text is None else work["best"]
    result = round_result(best, work["cand"], len(held))
    _count(run, "dropped_missing", result.dropped_missing)
    work["scored_any"] = bool(work["scored_any"]) or result.scored > 0
    if result.promoted is not None:
        run.candidate_text = run.cut[result.promoted]
        work["best"] = work["cand"][result.promoted]
    run.cut = []
    work["cand"], work["cands"] = [], []
    following = next_round(int(work["round"]), improved=result.promoted is not None)
    if following is None:
        await _finish(conn, run, deps)
        return
    work.update(round=following, phase="propose", half=0)
    await _advance(conn, run)


async def handle(args: AlignArgs, conn: Any, ctx: Any) -> None:
    if not _DEPS:
        raise NotWired("obs.align dependencies")
    await run_step(conn, args, _DEPS[0])


KIND_DECL: Final = JobKind(
    name=KIND,
    queue="obs",
    args=AlignArgs,
    key=("proposal_id", "step"),
    handler=handle,
    max_attempts=1,
    timeout_s=180,
)
