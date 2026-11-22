"""The per-key state machine and the status map, as data (SRV-EVENTS INV-EVENTS-6, FR-EVENTS-12).

The worker, the sweep and the API act on Postgres; these tables are the model the tests check those actions
against, and `job_status` reads its answer from `status_of`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

STATES: Final = ("none", "queued", "running", "succeeded", "dead")
EVENTS: Final = (
    "enqueue",
    "claim",
    "ok",
    "fail_transient",
    "fail_last",
    "fail_permanent",
    "stall",
    "stall_last",
    "stall_live",
    "abort",
    "requeue",
)


@dataclass(frozen=True)
class Step:
    next: str
    result: str
    effect: int
    attempts: int | str


_NOOP: Final = "noop"

# Explicit rows; every pair not listed is a no-op that keeps the state (VEC-EVENTS-1 lists those too).
_ROWS: Final[dict[tuple[str, str], Step]] = {
    ("none", "enqueue"): Step("queued", "queued", 0, 0),
    ("none", "requeue"): Step("none", "unknown_key", 0, 0),
    ("queued", "enqueue"): Step("queued", "already_queued", 0, 0),
    ("queued", "claim"): Step("running", "-", 0, 0),
    ("queued", "requeue"): Step("queued", "already_queued", 0, 0),
    ("running", "enqueue"): Step("running", "queued", 0, 0),
    ("running", "ok"): Step("succeeded", "-", 1, 0),
    ("running", "fail_transient"): Step("queued", "-", 0, 1),
    ("running", "fail_last"): Step("dead", "-", 0, 1),
    ("running", "fail_permanent"): Step("dead", "-", 0, 1),
    ("running", "stall"): Step("queued", "-", 0, 1),
    ("running", "stall_last"): Step("dead", "-", 0, 1),
    ("running", "stall_live"): Step("running", "-", 0, 0),
    ("running", "abort"): Step("queued", "-", 0, 0),
    ("running", "requeue"): Step("running", "running", 0, 0),
    ("succeeded", "enqueue"): Step("succeeded", "already_done", 0, 0),
    ("succeeded", "claim"): Step("succeeded", "skipped", 0, 0),
    ("succeeded", "stall"): Step("succeeded", "finished", 0, 0),
    ("succeeded", "stall_last"): Step("succeeded", "finished", 0, 0),
    ("succeeded", "requeue"): Step("succeeded", "already_done", 0, 0),
    ("dead", "enqueue"): Step("dead", "dead", 0, 0),
    ("dead", "claim"): Step("dead", "skipped", 0, 0),
    ("dead", "stall"): Step("dead", "finished", 0, 0),
    ("dead", "stall_last"): Step("dead", "finished", 0, 0),
    ("dead", "requeue"): Step("queued", "queued", 0, "reset"),
}


def transition(state: str, event: str) -> Step:
    if state not in STATES or event not in EVENTS:
        raise ValueError("unknown state or event")
    return _ROWS.get((state, event), Step(state, _NOOP, 0, 0))


# Deciding procrastinate job (FR-EVENTS-12) x outcome -> (job_status, reconcile action).
RECONCILE_ENQUEUE: Final = "enqueue"
RECONCILE_STALL_LIKE: Final = "stall_like"
RECONCILE_NONE: Final = "-"


def status_of(deciding: str, outcome: str | None, key_row: bool = True) -> tuple[str, str]:
    """`deciding` is a procrastinate status or `absent`; `outcome` is `succeeded`, `dead` or None."""
    if outcome in ("succeeded", "dead"):
        return outcome, RECONCILE_NONE
    if outcome is not None:
        raise ValueError("unknown outcome")
    if deciding in ("doing", "aborting"):
        return "running", RECONCILE_NONE
    if deciding == "todo":
        return "queued", RECONCILE_NONE
    if deciding in ("succeeded", "failed"):
        return "queued", RECONCILE_STALL_LIKE
    if deciding == "cancelled":
        return "none", RECONCILE_NONE
    if deciding == "aborted":
        return "queued", RECONCILE_ENQUEUE
    if deciding == "absent":
        return ("queued", RECONCILE_ENQUEUE) if key_row else ("none", RECONCILE_NONE)
    raise ValueError("unknown procrastinate status")


def deciding_status(statuses_by_id: list[tuple[int, str]]) -> str:
    """doing (or legacy aborting) > todo > the newest job by id > absent."""
    present = {s for _, s in statuses_by_id}
    if "doing" in present or "aborting" in present:
        return "doing" if "doing" in present else "aborting"
    if "todo" in present:
        return "todo"
    if not statuses_by_id:
        return "absent"
    return max(statuses_by_id)[1]
