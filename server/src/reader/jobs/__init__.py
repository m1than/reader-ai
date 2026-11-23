"""The job queue (SRV-EVENTS, jobs half): procrastinate delivery; per-key state in job_keys."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

from reader.jobs.catalog import KIND_MODULES, Catalog, load_kinds
from reader.jobs.model import JobContext, JobError, JobFailed, JobKind, Result, idempotency_key, lock_id

if TYPE_CHECKING:
    from reader.jobs.queue import Jobs

MIGRATIONS: Final = Path(__file__).resolve().parent / "migrations"


def running() -> Jobs:
    """The worker's `Jobs` (reader.jobs.worker.running, imported lazily: it pulls procrastinate)."""
    from reader.jobs.worker import running as _running

    return _running()


__all__ = [
    "KIND_MODULES",
    "MIGRATIONS",
    "Catalog",
    "JobContext",
    "JobError",
    "JobFailed",
    "JobKind",
    "Result",
    "idempotency_key",
    "load_kinds",
    "lock_id",
    "running",
]
