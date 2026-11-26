"""The worker lanes, declared as data (SRV-EVENTS FR-EVENTS-9 as amended for ADR-0039 B7b).

`main` serves every queue but `judge` at concurrency 1 and runs the sweep and the periodic entries; `judge` (the
`worker-judge` service, OPS-DEPLOY FR-DEPLOY-16) serves only `judge` at concurrency 4 and runs its own reaper.
Both load the full catalog; a lane only selects queues.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from reader.jobs.model import SWEEP_TASK, JobError

JUDGE_QUEUE: Final = "judge"
JUDGE_KINDS: Final = frozenset({"judge.find"})  # the only kinds allowed on the judge queue
POOL_EXTRA: Final = 4  # procrastinate's own connections: listener, fetch, heartbeat, periodic deferrer


@dataclass(frozen=True)
class Lane:
    name: str
    concurrency: int
    queues: tuple[str, ...] | None  # None: every catalog queue that no other lane names
    sweep: bool  # the start sweep, the sweep queue and its periodic entries
    hook_modules: tuple[str, ...] | None  # whose `on_worker_start` runs; None: every KIND_MODULES module

    @property
    def pool_size(self) -> int:
        """procrastinate's psycopg pool: one job-finish write per running job plus its own connections."""
        return self.concurrency + POOL_EXTRA


LANES: Final[Mapping[str, Lane]] = MappingProxyType(
    {
        "main": Lane("main", 1, None, True, None),
        "judge": Lane("judge", 4, (JUDGE_QUEUE,), False, ("reader.agent.lane",)),
    }
)


def lane_of(name: str, lanes: Mapping[str, Lane] = LANES) -> Lane:
    lane = lanes.get(name)
    if lane is None:
        raise JobError("lane_unknown")
    return lane


def claimed(lanes: Mapping[str, Lane] = LANES) -> frozenset[str]:
    return frozenset(q for lane in lanes.values() if lane.queues is not None for q in lane.queues)


def lane_queues(lane: Lane, catalog_queues: Iterable[str], lanes: Mapping[str, Lane] = LANES) -> list[str]:
    """The queues a lane's worker serves."""
    if lane.queues is not None:
        queues = list(lane.queues)
    else:
        taken = claimed(lanes)
        queues = [q for q in catalog_queues if q not in taken]
    return [*queues, SWEEP_TASK] if lane.sweep else queues


def check_served(catalog_queues: Iterable[str], lanes: Mapping[str, Lane] = LANES) -> None:
    """Every catalog queue is served by some lane (`queue_unserved` otherwise)."""
    queues = list(catalog_queues)
    served = {q for lane in lanes.values() for q in lane_queues(lane, queues, lanes)}
    if any(q not in served for q in queues):
        raise JobError("queue_unserved")
