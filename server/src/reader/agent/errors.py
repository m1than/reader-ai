"""Agent errors as data (SRV-AGENT FR-AGENT-10): every code is fixed text, never a prompt or a reply.

SRV-EVENTS retries a handler failure only when its exception type is in the kind's `transient` tuple, so the
transient codes get their own class.
"""

from __future__ import annotations

from typing import Final

TRANSIENT: Final = frozenset(
    {"provider_unavailable", "rate_limited", "output_invalid", "upstream_retry", "deadline_exceeded"}
)
BUDGET: Final = frozenset(
    {"budget_exhausted", "task_budget_exhausted", "monthly_budget_exhausted", "daily_budget_exhausted"}
)


class AgentError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TransientAgentError(AgentError):
    """Retried by SRV-EVENTS within the kind's max_attempts."""


class BudgetRefused(AgentError):
    """Refused before any reservation row: a stage outcome, not a failure (SRV-PIPELINE FR-PIPELINE-5)."""


def agent_error(code: str) -> AgentError:
    if code in TRANSIENT:
        return TransientAgentError(code)
    if code in BUDGET:
        return BudgetRefused(code)
    return AgentError(code)
