"""Spike tutor-call: 5 tutor-shaped single-turn calls with the Agent SDK; record tokens (output, thinking), wall time
and cost per call (SRV-TUTOR review round 1, blocker 1).

Inside the container:  python /app/call.py
  /in/sources.json   the retrieved spans for each question: synthetic by default (spikes/tutor-call/synthetic.json)
  /out/numbers/      one JSON per call plus summary.json (numbers only; committed)
Env:
  CAP_REMAINING   USD left of the spike's cap; the run stops before a call when less than MIN_CALL_USD is left
  THINKING        "off" sets MAX_THINKING_TOKENS=0 for the CLI (the setting SRV-TUTOR asks SRV-AGENT to expose);
                  "default" leaves the CLI default. The run does both modes for each question.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query

MODEL = "claude-sonnet-5-5"
MIN_CALL_USD = 0.05
OUT = Path("/out/numbers")
SYSTEM = (Path(__file__).parent / "system.md").read_text()


async def one(question: dict, thinking: str, budget: float) -> dict:
    env = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1",
           "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "8000"}
    if thinking == "off":
        env["MAX_THINKING_TOKENS"] = "0"
    prompt = json.dumps({"question": question["question"], "cell": question.get("cell"),
                         "sources": question["sources"]}, ensure_ascii=False)
    opts = ClaudeAgentOptions(system_prompt=SYSTEM, tools=[], allowed_tools=[], setting_sources=[], max_turns=1,
                              model=MODEL, max_budget_usd=budget, env=env)
    t0 = time.monotonic()
    text, result = "", None
    async for msg in query(prompt=prompt, options=opts):
        if isinstance(msg, AssistantMessage):
            text += "".join(b.text for b in msg.content if isinstance(b, TextBlock))
        elif isinstance(msg, ResultMessage):
            result = msg
    wall = time.monotonic() - t0
    usage = (result.usage or {}) if result else {}
    try:
        parsed = json.loads(text.strip().removeprefix("```json").removesuffix("```").strip())
        json_ok = isinstance(parsed, dict) and "blocks" in parsed
    except ValueError:
        json_ok = False
    return {"question": question["id"], "thinking": thinking, "model": MODEL, "wall_s": round(wall, 2),
            "total_cost_usd": getattr(result, "total_cost_usd", None), "usage": usage, "output_chars": len(text),
            "json_ok": json_ok, "subtype": getattr(result, "subtype", None)}


async def main() -> None:
    questions = json.loads(Path("/in/sources.json").read_text())["questions"]
    left = float(os.environ["CAP_REMAINING"])
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for q in questions:
        for thinking in ("off", "default"):
            if left < MIN_CALL_USD:
                print("cap reached", file=sys.stderr)
                break
            row = await one(q, thinking, min(0.5, left))
            rows.append(row)
            left -= row["total_cost_usd"] or 0
            (OUT / f"{q['id']}-{thinking}.json").write_text(json.dumps(row, indent=1))
    (OUT / "summary.json").write_text(json.dumps(
        {"calls": len(rows), "total_usd": round(sum(r["total_cost_usd"] or 0 for r in rows), 6)}, indent=1))


asyncio.run(main())
