"""The query gate (FR-FRESH-6, VEC-FRESH-5): the search run's PreToolUse hook.

It allows a `WebSearch` call only when its input is exactly `{query}`, the query trimmed equals the planned
query, no domain list (or any other key) is present, and no call was allowed earlier in this run. Anything
else is denied with a fixed reason that never echoes the query.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

TOOL: Final = "WebSearch"
DENY_REASON: Final = "Call WebSearch once, with exactly the query you were given and no other parameters."


def _decision(allow: bool) -> dict[str, Any]:
    decision = "allow" if allow else "deny"
    output: dict[str, Any] = {"hookEventName": "PreToolUse", "permissionDecision": decision}
    if not allow:
        output["permissionDecisionReason"] = DENY_REASON
    return {"hookSpecificOutput": output}


@dataclass
class Gate:
    query: str
    allowed: int = 0
    denied: int = 0

    def decide(self, tool_name: object, tool_input: object) -> bool:
        ok = (
            tool_name == TOOL
            and isinstance(tool_input, Mapping)
            and set(tool_input) == {"query"}
            and isinstance(tool_input.get("query"), str)
            and tool_input["query"].strip() == self.query
            and self.allowed == 0
        )
        if ok:
            self.allowed += 1
        else:
            self.denied += 1
        return ok

    async def __call__(self, input_data: Any, tool_use_id: Any, context: Any) -> dict[str, Any]:
        data = input_data if isinstance(input_data, Mapping) else {}
        return _decision(self.decide(data.get("tool_name"), data.get("tool_input")))
