"""The job kinds the worker serves, built from a declared list of kind modules (SRV-EVENTS FR-EVENTS-1).

Each module named in `KIND_MODULES` exports `JOB_KINDS: tuple[JobKind, ...]`. SRV-INGEST and SRV-SEARCH add
their module names here (a one-line change each, with their spec).
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from typing import Any, Final

from reader.jobs.model import NAME_PATTERN, SWEEP_TASK, JobError, JobKind, model_has_secret_field

SWEEP_PRIORITY: Final = 1_000_000  # jobs.sweep runs before every kind; a kind's priority stays below it

KIND_MODULES: Final[tuple[str, ...]] = (
    "reader.ingest.jobs",  # SRV-INGEST paired change (FR-EVENTS-1)
    "reader.search.jobs",
    "reader.pipeline.jobs",  # SRV-PIPELINE paired change
    "reader.tutor.jobs",  # SRV-TUTOR paired change (FR-EVENTS-1)
    "reader.obs.jobs",  # SRV-OBS paired change (FR-EVENTS-19): obs.retention
    "reader.freshness.jobs",  # SRV-FRESH paired change (FR-EVENTS-1)
)
StartHook = Callable[[Any], Awaitable[None]]


class Catalog(Mapping[str, JobKind]):
    def __init__(self, kinds: Iterable[JobKind]) -> None:
        by_name: dict[str, JobKind] = {}
        for kind in kinds:
            if not re.fullmatch(NAME_PATTERN, kind.name):
                raise JobError("kind_name_invalid")
            if kind.name == SWEEP_TASK:
                raise JobError("kind_name_reserved")
            if kind.name in by_name:
                raise JobError("kind_name_duplicate")
            if kind.args.model_config.get("extra") != "forbid":
                raise JobError("kind_args_not_forbid")
            if not kind.key or any(f not in kind.args.model_fields for f in kind.key):
                raise JobError("kind_key_field_missing")
            if model_has_secret_field(kind.args):
                raise JobError("secret_in_payload")
            if kind.max_attempts < 1 or kind.timeout_s <= 0 or not 0 <= kind.priority < SWEEP_PRIORITY:
                raise JobError("kind_bounds_invalid")
            by_name[kind.name] = kind
        self._kinds = by_name

    def __getitem__(self, name: str) -> JobKind:
        return self._kinds[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._kinds)

    def __len__(self) -> int:
        return len(self._kinds)

    @property
    def queues(self) -> list[str]:
        return sorted({k.queue for k in self._kinds.values()})


def load_kinds(modules: Iterable[str] = KIND_MODULES) -> Catalog:
    kinds: list[JobKind] = []
    for name in modules:
        kinds.extend(importlib.import_module(name).JOB_KINDS)
    return Catalog(kinds)


def start_hooks(modules: Iterable[str] = KIND_MODULES) -> list[StartHook]:
    """Each kind module's optional `on_worker_start(jobs)` (paired SRV-PIPELINE amendment)."""
    hooks: list[StartHook] = []
    for name in modules:
        hook = getattr(importlib.import_module(name), "on_worker_start", None)
        if callable(hook):
            hooks.append(hook)
    return hooks
