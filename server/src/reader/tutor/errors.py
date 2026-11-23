"""The tutor's error catalogue and how SRV-AGENT's codes end a question (SRV-TUTOR `errors`, FR-TUTOR-3, -7).

Codes are fixed text, never a value from a question, a source or a model reply.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

CODES: Final = (
    "question_invalid",
    "cell_unknown",
    "unknown_revision",
    "not_found",
    "tutor_busy",
    "daily_budget_exhausted",
    "budget_exhausted",
    "monthly_budget_exhausted",
    "provider_missing",
    "sources_unavailable",
    "output_invalid",
    "output_truncated",
    "provider_unavailable",
    "rate_limited",
    "expired",
    "lost",
    "tutor_failed",
)
# Retried once by SRV-EVENTS (FR-TUTOR-3): the tutor's own output_invalid and SRV-AGENT's transient codes.
TRANSIENT: Final = frozenset(
    {"provider_unavailable", "rate_limited", "output_invalid", "upstream_retry", "deadline_exceeded"}
)
# SRV-AGENT codes stored under another catalogue code (FR-TUTOR-3, -7).
RENAMED: Final[Mapping[str, str]] = {
    "upstream_retry": "provider_unavailable",  # SRV-AGENT does not say which status was being retried
    "deadline_exceeded": "provider_unavailable",
    "task_budget_exhausted": "budget_exhausted",  # SRV-AGENT's per-run task budget: here per question
}
REFUSED: Final = frozenset({"budget_exhausted", "daily_budget_exhausted", "monthly_budget_exhausted"})


class TutorError(Exception):
    """A permanent outcome: the question ends with `code`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TutorTransient(Exception):
    """Retried by SRV-EVENTS before the last attempt (the kind's `transient`); `code` is a catalogue code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def outcome(code: str) -> tuple[str, str]:
    """The terminal (state, error code) of a question that ended with `code` (any source)."""
    stored = RENAMED.get(code, code)
    if stored not in CODES:
        stored = "tutor_failed"
    return ("refused" if stored in REFUSED else "failed"), stored
