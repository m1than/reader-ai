"""Agent errors as data (SRV-AGENT FR-AGENT-10): every code is fixed text, never a prompt or a reply.

SRV-EVENTS retries a handler failure only when its exception type is in the kind's `transient` tuple, so the
transient codes get their own class. ERROR_CLASSES gives every code the adapter raises exactly one class
(ADR-0039 revision); agent-proxy's forwarding refusals only appear in a run_token's record (→ `sdk_error`),
and its issue answers are mapped by PROXY_CODES (FR-AGENT-6).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

TRANSIENT: Final = frozenset(
    {
        "provider_unavailable",
        "rate_limited",
        "output_invalid",
        "upstream_retry",
        "deadline_exceeded",
        "lane_saturated",
    }
)
BUDGET: Final = frozenset(
    {
        "budget_exhausted",
        "task_budget_exhausted",
        "monthly_budget_exhausted",
        "daily_budget_exhausted",
        "index_budget_exhausted",
        "eval_budget_exhausted",
    }
)
PERMANENT: Final = frozenset(
    {
        "sdk_error",
        "tools_mismatch",
        "unknown_model",
        "provider_missing",
        "usage_unmapped",
        "cap_missing",
        "line_invalid",
        "replay_miss",
        "judge_window_too_long",
        "judge_input_too_large",
    }
)
ERROR_CLASSES: Final[Mapping[str, str]] = MappingProxyType(
    {
        **dict.fromkeys(BUDGET, "budget"),
        **dict.fromkeys(PERMANENT, "permanent"),
        **dict.fromkeys(TRANSIENT, "transient"),
    }
)
# agent-proxy's refusals of a forwarded request (FR-AGENT-4, -15, -16): recorded, never raised as themselves.
PROXY_REFUSALS: Final = frozenset(
    {
        "run_token_unknown",
        "model_mismatch",
        "output_cap",
        "body_not_json",
        "body_not_utf8",
        "tools_not_allowed",
        "search_cap",
        "request_cap",
    }
)
# agent-proxy's answers to a run_token issue → the adapter's code (FR-AGENT-6).
PROXY_CODES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "client_quota": "lane_saturated",
        "client_unknown": "sdk_error",
        "tools_not_allowed": "sdk_error",
        "bad_request": "sdk_error",
    }
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
