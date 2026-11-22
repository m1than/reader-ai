"""The job kinds the worker serves, built from a declared list of kind modules (SRV-EVENTS FR-EVENTS-1).

Each module named in `KIND_MODULES` exports `JOB_KINDS: tuple[JobKind, ...]`. SRV-INGEST and SRV-SEARCH add
their module names here (a one-line change each, with their spec).
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Iterable, Iterator, Mapping
from typing import Final

from reader.jobs.model import NAME_PATTERN, SWEEP_TASK, JobError, JobKind, model_has_secret_field

KIND_MODULES: Final[tuple[str, ...]] = ()


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
            if not kind.key or any(f not in kind.args.model_fields for f in kind.key):
                raise JobError("kind_key_field_missing")
            if model_has_secret_field(kind.args):
                raise JobError("secret_in_payload")
            if kind.max_attempts < 1 or kind.timeout_s <= 0:
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
