"""Spike probe (never shipped): run the Agent SDK's bundled CLI against the local stub with a handle-shaped token.

Records the init SystemMessage's tool list under tools=[] and every message type, then writes /out/probe.json.
No real token, no network (the container runs with --network none), no credit.
"""

import asyncio
import json
import os
import subprocess
import sys
import time

import claude_agent_sdk
from claude_agent_sdk import ClaudeAgentOptions, query


async def run(mode: str) -> dict:
    opts = ClaudeAgentOptions(
        tools=[] if mode == "tools-empty" else None,
        allowed_tools=[],
        setting_sources=[],
        max_turns=1,
        model="claude-sonnet-5-5",
        env={
            "CLAUDE_CODE_OAUTH_TOKEN": os.environ["HANDLE"],
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:8080",
            "HTTP_PROXY": "http://127.0.0.1:8081",
            "HTTPS_PROXY": "http://127.0.0.1:8081",
            "NO_PROXY": "127.0.0.1",
            "HOME": os.environ["HOME"],
            "PATH": os.environ["PATH"],
            "LANG": "C.UTF-8",
            "DISABLE_TELEMETRY": "1",
            **({"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"} if os.environ.get("NONESSENTIAL_OFF") else {}),
            **({"CLAUDE_CODE_MAX_OUTPUT_TOKENS": os.environ["MAX_OUT"]} if os.environ.get("MAX_OUT") else {}),
        },
    )
    out: dict = {"mode": mode, "messages": [], "init_tools": None, "error": None}
    t0 = time.time()
    try:
        async for msg in query(prompt="Reply with the word ok.", options=opts):
            kind = type(msg).__name__
            entry = {"type": kind, "t": round(time.time() - t0, 3)}
            if kind == "AssistantMessage":
                entry["error"] = getattr(msg, "error", None)
            if kind == "SystemMessage":
                entry["subtype"] = getattr(msg, "subtype", None)
                data = getattr(msg, "data", {}) or {}
                entry["data_keys"] = sorted(data.keys())
                if getattr(msg, "subtype", None) == "init":
                    out["init_tools"] = data.get("tools")
                    out["init_model"] = data.get("model")
                    out["init_mcp_servers"] = data.get("mcp_servers")
            if kind == "ResultMessage":
                entry["subtype"] = getattr(msg, "subtype", None)
                entry["is_error"] = getattr(msg, "is_error", None)
                entry["total_cost_usd"] = getattr(msg, "total_cost_usd", None)
                entry["usage_keys"] = sorted((getattr(msg, "usage", None) or {}).keys())
                entry["usage"] = getattr(msg, "usage", None)
                entry["result"] = getattr(msg, "result", None)
                entry["fields"] = {k: v for k, v in vars(msg).items() if k not in ("usage", "model_usage")}
            out["messages"].append(entry)
    except Exception as exc:  # recorded, not raised: the spike reports what happened
        out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    out["wall_s"] = round(time.time() - t0, 2)
    return out


def main() -> None:
    version = getattr(claude_agent_sdk, "__version__", "?")
    try:
        cli = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    except OSError:
        cli = "not on PATH"
    results = {"sdk_version": version, "cli_on_path": cli, "python": sys.version.split()[0], "runs": []}
    for mode in sys.argv[1:] or ["tools-empty"]:
        results["runs"].append(asyncio.run(run(mode)))
    with open("/out/probe.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(json.dumps({"sdk": version, "runs": [(r["mode"], r["init_tools"], r["error"]) for r in results["runs"]]}))


if __name__ == "__main__":
    main()
