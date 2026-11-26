"""Job kinds, idempotency keys, the key lock id and payload checks (SRV-EVENTS FR-EVENTS-1, -2, -10, -13)."""

from __future__ import annotations

import hashlib
import json
import types
import typing
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncConnection

from reader.config import Secret

NAME_PATTERN: Final = r"^[a-z][a-z0-9_.]{2,63}$"
SWEEP_TASK: Final = "jobs.sweep"
# The judge lane's collectors (FR-EVENTS-34); periodic tasks like the sweep, never `JobKind`s.
JUDGE_REAP_TASK: Final = "jobs.judge_reap"
JUDGE_GC_TASK: Final = "jobs.judge_gc"
TICK_SUFFIX: Final = ".tick"  # `<kind>.tick`: a kind's periodic entry (FR-EVENTS-32)
RESERVED_TASKS: Final = frozenset({SWEEP_TASK, JUDGE_REAP_TASK, JUDGE_GC_TASK})
LOCK_NAMESPACE: Final = b"reader.jobs\0"


class JobError(Exception):
    """A rejected call; `code` is fixed text, never a value from the payload."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class JobFailed(Exception):
    """Raised by the worker wrapper instead of a handler's exception: carries only the error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class JobContext:
    job_id: int
    key: str
    attempts: int


Handler = Callable[[Any, AsyncConnection, JobContext], Awaitable[None]]


@dataclass(frozen=True)
class JobKind:
    name: str
    queue: str
    args: type[BaseModel]
    key: tuple[str, ...]
    handler: Handler
    max_attempts: int = 3
    transient: tuple[type[BaseException], ...] = ()
    timeout_s: float = 600.0
    priority: int = 0  # procrastinate job priority at every defer; higher first (FR-EVENTS-1 amendment)
    traced: bool = True  # FR-EVENTS-32 (SRV-OBS FR-OBS-5 f): False opens no job root span and passes no trace
    # A periodic entry (SRV-OBS FR-OBS-32): (cron, args at the tick's UTC time); each tick enqueues the kind
    # once, so the key built from those args deduplicates ticks of one slot.
    periodic: tuple[str, Callable[[Any], Mapping[str, Any]]] | None = None
    # Called once after the transaction that set the key's outcome ("succeeded" or "dead") committed, with
    # (engine, payload, outcome) (SRV-PIPELINE FR-PIPELINE-18, SRV-INGEST FR-INGEST-26). A failure is logged,
    # never judged: the outcome is already recorded.
    after_end: Callable[[Any, Mapping[str, Any], str], Awaitable[None]] | None = None
    # FR-EVENTS-11 (amended): finished `job_keys` rows of this kind older than this are deleted by the collector.
    key_retention_s: int | None = None


@dataclass(frozen=True)
class Result:
    """What enqueue and requeue_dead return: `kind` is one of the FR-EVENTS-3 / -8 result names."""

    kind: str
    job_id: int | None = field(default=None)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def idempotency_key(kind: JobKind, payload: Mapping[str, Any]) -> str:
    fields = {name: payload[name] for name in kind.key}
    digest = hashlib.sha256(canonical_json(fields).encode("utf-8")).hexdigest()
    return f"{kind.name}:{digest}"


def lock_id(key: str) -> int:
    """First 8 bytes of sha256(namespace + key), a signed big-endian int64 (one-bigint advisory lock form)."""
    digest = hashlib.sha256(LOCK_NAMESPACE + key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _mentions_secret(annotation: Any, seen: set[int]) -> bool:
    if isinstance(annotation, type) and issubclass(annotation, Secret):
        return True
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if id(annotation) in seen:
            return False
        seen.add(id(annotation))
        return any(_mentions_secret(f.annotation, seen) for f in annotation.model_fields.values())
    origin = typing.get_origin(annotation)
    if origin is not None or isinstance(annotation, types.UnionType):
        return any(_mentions_secret(a, seen) for a in typing.get_args(annotation))
    return False


def model_has_secret_field(model: type[BaseModel]) -> bool:
    return _mentions_secret(model, set())


def _contains_secret_instance(value: Any) -> bool:
    if isinstance(value, Secret):
        return True
    if isinstance(value, Mapping):
        return any(_contains_secret_instance(k) or _contains_secret_instance(v) for k, v in value.items())
    if isinstance(value, list | tuple | set | frozenset):
        return any(_contains_secret_instance(v) for v in value)
    if isinstance(value, BaseModel):
        return any(_contains_secret_instance(getattr(value, n)) for n in type(value).model_fields)
    return False


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def check_payload(raw: Any, payload: Mapping[str, Any], secret_values: Iterable[str]) -> None:
    """FR-EVENTS-10: no `Secret` instance in the input, no resolved secret value in any string leaf or key."""
    if _contains_secret_instance(raw):
        raise JobError("secret_in_payload")
    values = list(secret_values)
    assert all(values), "resolved secret values are never empty (SRV-CONFIG)"  # noqa: S101
    values = [v for v in values if v]
    decoded = json.loads(json.dumps(payload))
    for text in _strings(decoded):
        if any(v in text for v in values):
            raise JobError("secret_in_payload")
