"""Compile one lesson through ADR-0009's stages with the Agent SDK and record cost, tokens, turns and time per stage.

Inside the container:  python /app/pipeline.py <lesson.json>
  /in/source/*.md   the lesson's source pages (copyrighted; read-only mount; never written back)
  /out/text/        stage outputs (lesson text; the host dir is gitignored)
  /out/numbers/     one JSON per stage plus summary.json (numbers only; committed)
Env: CAP_REMAINING (USD left of the spike's cap). Each stage gets max_budget_usd = min(its ADR-0011 budget,
what is left), and the pipeline stops before a stage when less than MIN_STAGE_USD is left.
"""

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query

import prompts

OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5-5"
# Task classes and per-lesson budgets from ADR-0011's reader.yaml example (draft opus 2.0, fact_check sonnet 1.0,
# review opus 1.2); revise is the draft task class again (ADR-0009: findings loop back to stage 3).
STAGES = [
    ("1-draft", OPUS, 2.0), ("2-fact_check", SONNET, 1.0), ("3-review", OPUS, 1.2),
    ("4-revise", OPUS, 2.0), ("5-fact_check", SONNET, 1.0), ("6-review", OPUS, 1.2),
]
MIN_STAGE_USD = 0.5
NO_TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch", "Task", "TodoWrite",
            "NotebookEdit"]
OUT = Path("/out")


def source_text() -> str:
    pages = sorted(Path("/in/source").glob("p*.md"))
    return "\n\n".join(f"=== PDF page {int(p.stem[1:])} ===\n{p.read_text()}" for p in pages)


def as_json(text: str) -> tuple[object | None, str]:
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(body), ""
    except json.JSONDecodeError as e:
        return None, f"{e.msg} at {e.pos}"


async def run_stage(name: str, model: str, budget: float, prompt: str) -> dict[str, object]:
    t0 = time.monotonic()
    text, result = "", None
    # tools=[] gives the model no tools at all (ClaudeAgentOptions.tools in 0.2.186); the denylist stays as a second
    # layer. The two recorded runs used the denylist only (tools=[] was added after them, without a new run).
    options = ClaudeAgentOptions(model=model, max_turns=3, tools=[], allowed_tools=[], disallowed_tools=NO_TOOLS,
                                 setting_sources=[], max_budget_usd=budget)
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            text += "".join(b.text for b in msg.content if isinstance(b, TextBlock))
        elif isinstance(msg, ResultMessage):
            result = msg
    parsed, err = as_json(text)
    (OUT / "text" / f"{name}.json").write_text(json.dumps(parsed, ensure_ascii=False, indent=1) if parsed else text)
    row = {
        "stage": name, "model": model, "max_budget_usd": round(budget, 4),
        "wall_s": round(time.monotonic() - t0, 1),
        "total_cost_usd": getattr(result, "total_cost_usd", None), "usage": getattr(result, "usage", None),
        "num_turns": getattr(result, "num_turns", None), "duration_api_ms": getattr(result, "duration_api_ms", None),
        "subtype": getattr(result, "subtype", None), "is_error": getattr(result, "is_error", None),
        "output_chars": len(text), "json_ok": parsed is not None, "json_error": err,
    }
    if isinstance(parsed, dict):
        cells = parsed.get("cells")
        if isinstance(cells, list):
            row["cells"] = len(cells)
            row["claims"] = sum(len(c.get("claims", [])) for c in cells if isinstance(c, dict))
            row["kinds"] = sorted({c.get("kind") for c in cells if isinstance(c, dict)} - {None})
        for key in ("claims_checked", "supported", "verdict"):
            if key in parsed:
                row[key] = parsed[key]
        for key in ("miscited", "unsupported", "contradicted", "findings"):
            if isinstance(parsed.get(key), list):
                row[f"n_{key}"] = len(parsed[key])
        if isinstance(parsed.get("findings"), list):
            row["n_blocking"] = sum(1 for f in parsed["findings"] if isinstance(f, dict)
                                    and f.get("severity") == "blocking")
    (OUT / "numbers" / f"{name}.json").write_text(json.dumps(row, indent=1))
    return {"row": row, "text": json.dumps(parsed, ensure_ascii=False) if parsed else text}


async def main(lesson_path: str) -> None:
    lesson = json.loads(Path(lesson_path).read_text())
    (OUT / "text").mkdir(parents=True, exist_ok=True)
    (OUT / "numbers").mkdir(parents=True, exist_ok=True)
    src = source_text()
    remaining = float(os.environ["CAP_REMAINING"])
    t0 = time.monotonic()
    rows: list[dict[str, object]] = []
    notebook = fact_check = review = ""
    stopped = None
    for name, model, budget in STAGES:
        if remaining < MIN_STAGE_USD:
            stopped = f"cap: ${remaining:.2f} left before {name}"
            break
        kind = name.split("-", 1)[1]
        fill = {"source": src, "kinds": prompts.CELL_KINDS, "notebook": notebook, "fact_check": fact_check,
                "review": review, "lesson_id": lesson["id"], "title": lesson["title"],
                "sections": "; ".join(lesson["sections"])}
        template = {"draft": prompts.DRAFT, "fact_check": prompts.FACT_CHECK, "review": prompts.REVIEW,
                    "revise": prompts.REVISE}[kind]
        out = await run_stage(name, model, min(budget, remaining), template.format(**fill))
        row = out["row"]
        rows.append(row)
        remaining -= float(row["total_cost_usd"] or 0)
        print(json.dumps({"stage": name, "usd": row["total_cost_usd"], "wall_s": row["wall_s"],
                          "spent_this_run": round(sum(float(r["total_cost_usd"] or 0) for r in rows), 4),
                          "cap_left": round(remaining, 4)}), flush=True)
        if kind in ("draft", "revise"):
            notebook = out["text"]
        elif kind == "fact_check":
            fact_check = out["text"]
        else:
            review = out["text"]
        if row["is_error"]:
            stopped = f"{name}: {row['subtype']}"
            break
    summary = {
        "lesson": lesson["id"], "source_chars": len(src), "source_pages": len(list(Path("/in/source").glob("p*.md"))),
        "stages": len(rows), "stopped": stopped, "wall_s": round(time.monotonic() - t0, 1),
        "total_cost_usd": round(sum(float(r["total_cost_usd"] or 0) for r in rows), 4),
        "first_pass_usd": round(sum(float(r["total_cost_usd"] or 0) for r in rows[:3]), 4),
    }
    (OUT / "numbers" / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary), flush=True)


asyncio.run(main(sys.argv[1]))
