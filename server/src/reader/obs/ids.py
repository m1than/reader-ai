"""Derived job trace ids (SRV-OBS FR-OBS-5): a job's root span has trace id = the first 16 bytes of
sha256("reader.job" NUL <job id as decimal ASCII> NUL <idempotency key UTF-8>), bytes 16..31 when those are
all zero. Every other span gets a random id. The id generator reads a context variable set only around the
root's creation."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Final

from opentelemetry.sdk.trace.id_generator import RandomIdGenerator

_NEXT: Final[ContextVar[int | None]] = ContextVar("reader_obs_next_trace_id", default=None)


def derive_trace_id(job_id: int, key: str) -> int:
    digest = hashlib.sha256(
        b"reader.job\x00" + str(int(job_id)).encode("ascii") + b"\x00" + key.encode("utf-8")
    ).digest()
    first = digest[:16]
    return int.from_bytes(first if any(first) else digest[16:32], "big")


@contextmanager
def next_trace_id(value: int) -> Iterator[None]:
    token = _NEXT.set(value)
    try:
        yield
    finally:
        _NEXT.reset(token)


class JobIdGenerator(RandomIdGenerator):
    def generate_trace_id(self) -> int:
        value = _NEXT.get()
        return value if value else super().generate_trace_id()

    def is_trace_id_random(self) -> bool:
        """False: a job's trace id is derived, so no span claims W3C's random-trace-id flag (flags 01)."""
        return False
