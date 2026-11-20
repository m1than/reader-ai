"""Spike probe: one short query through the Agent SDK; prints one JSON object with timings, usage and cost.
Never prints the environment or the token.

    python probe.py <model>
"""

import asyncio
import json
import sys
import time

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, SystemMessage, TextBlock, query
from claude_agent_sdk.types import StreamEvent

PROMPT = "Count from 1 to 20 in words, comma-separated, nothing else."


async def main(model: str) -> None:
    t0 = time.monotonic()
    marks: dict[str, float] = {}
    deltas = 0
    text = ""
    result: dict[str, object] = {}
    options = ClaudeAgentOptions(model=model, max_turns=1, allowed_tools=[], disallowed_tools=[
        "Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch", "Task", "TodoWrite", "NotebookEdit"],
        include_partial_messages=True, setting_sources=[])
    async for msg in query(prompt=PROMPT, options=options):
        now = round(time.monotonic() - t0, 3)
        if isinstance(msg, SystemMessage) and "init" not in marks:
            marks["init"] = now
        elif isinstance(msg, StreamEvent):
            ev = msg.event
            if ev.get("type") == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
                deltas += 1
                marks.setdefault("first_token", now)
        elif isinstance(msg, AssistantMessage):
            text += "".join(b.text for b in msg.content if isinstance(b, TextBlock))
        elif isinstance(msg, ResultMessage):
            marks["result"] = now
            result = {"is_error": msg.is_error, "total_cost_usd": msg.total_cost_usd, "usage": msg.usage,
                      "duration_ms": msg.duration_ms, "duration_api_ms": msg.duration_api_ms, "num_turns": msg.num_turns}
    print(json.dumps({"model": model, "marks_s": marks, "text_deltas": deltas,
                      "answer_ok": "twenty" in text.lower(), "answer_chars": len(text), **result}))


asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "claude-haiku-5-5"))
