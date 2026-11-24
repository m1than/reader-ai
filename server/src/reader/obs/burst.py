"""SRV-OBS FR-OBS-35 (ADR-0038): the load limits an opt-in [image] burst test is to check.

The limits are compose's memory limits per member (deploy/compose.yaml); the burst test fails on a dropped
span, an ingest p95 above MAX_P95_S, or a peak above MAX_PEAK_RATIO of a limit. The replay is not built yet:
FR-OBS-35 is "not verified until implemented", and the replay, when built, is the test's own code, never
shipped."""

from __future__ import annotations

from typing import Final

LIMITS: Final = {"mlflow": 1024**3, "rotel": 256 * 1024**2, "jaeger": 256 * 1024**2}
MAX_P95_S: Final = 30
MAX_PEAK_RATIO: Final = 0.8
LESSONS, JOBS_PER_LESSON, SPANS_PER_JOB = 60, 8, 72  # round 9's whole book
QUESTIONS, SPANS_PER_QUESTION = 100, 10
