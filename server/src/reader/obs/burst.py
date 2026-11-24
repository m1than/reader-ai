"""SRV-OBS FR-OBS-35 (ADR-0038): the load limits the opt-in [image] burst test checks.

The limits are compose's memory limits per member (deploy/compose.yaml); the test fails on a dropped span, an
ingest p95 above MAX_P95_S, or a peak above MAX_PEAK_RATIO of a limit. The replay itself (`run`) is not
implemented yet: the burst test is opt-in (READER_TEST_BURST=1) and stays skipped until it is."""

from __future__ import annotations

from typing import Any, Final

LIMITS: Final = {"mlflow": 1024**3, "rotel": 256 * 1024**2, "jaeger": 256 * 1024**2}
MAX_P95_S: Final = 30
MAX_PEAK_RATIO: Final = 0.8
LESSONS, JOBS_PER_LESSON, SPANS_PER_JOB = 60, 8, 72  # round 9's whole book
QUESTIONS, SPANS_PER_QUESTION = 100, 10


def run(rotel: str, mlflow: str) -> dict[str, Any]:
    """Replay round 9's burst against `rotel` and count what `mlflow` stored (FR-OBS-35)."""
    raise NotImplementedError("the FR-OBS-35 burst replay is not implemented yet")
