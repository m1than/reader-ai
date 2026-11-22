"""The job queue (SRV-EVENTS, jobs half): procrastinate delivery; per-key state in job_keys."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from reader.jobs.catalog import KIND_MODULES, Catalog, load_kinds
from reader.jobs.model import JobContext, JobError, JobFailed, JobKind, Result, idempotency_key, lock_id

MIGRATIONS: Final = Path(__file__).resolve().parent / "migrations"

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
]
