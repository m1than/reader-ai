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


HOOK_CALLS: list = []


def hook_for(mode: str):  # type: ignore[no-untyped-def]
    """PreToolUse hook (the adapter passes a string prompt, so a hook, not can_use_tool): records and decides."""
    async def hook(input_data, tool_use_id, context):  # type: ignore[no-untyped-def]
        HOOK_CALLS.append({"tool_name": input_data.get("tool_name"), "tool_input": input_data.get("tool_input"),
                           "keys": sorted(input_data.keys())})
        decision = "deny" if mode == "websearch-deny" else "allow"
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                       "permissionDecisionReason": "spike gate"}}
    return hook


def block_summary(block: object) -> dict:
    e = {"type": type(block).__name__}
    for k in ("name", "input", "id", "tool_use_id", "is_error"):
        if hasattr(block, k):
            e[k] = getattr(block, k)
    if hasattr(block, "text"):
        e["text"] = str(getattr(block, "text"))[:300]
    if hasattr(block, "content"):
        e["content"] = json.dumps(getattr(block, "content"), default=str, ensure_ascii=False)[:1500]
    return e


async def run(mode: str) -> dict:
    websearch = mode.startswith("websearch")
    extra: dict = {}
    if websearch:
        from claude_agent_sdk import HookMatcher
        extra = {"hooks": {"PreToolUse": [HookMatcher(matcher="WebSearch", hooks=[hook_for(mode)])]}}
    opts = ClaudeAgentOptions(
        tools=["WebSearch"] if websearch else ([] if mode == "tools-empty" else None),
        allowed_tools=[],
        setting_sources=[],
        max_turns=4 if websearch else 1,
        model=os.environ.get("MODEL", "claude-sonnet-5-5"),
        **extra,
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
            **({"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
               if os.environ.get("NONESSENTIAL_OFF") or websearch else {}),
            **({"CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION": "2", "MAX_THINKING_TOKENS": "0"} if websearch else {}),
            **({"CLAUDE_CODE_MAX_OUTPUT_TOKENS": os.environ["MAX_OUT"]} if os.environ.get("MAX_OUT") else {}),
        },
    )
    out: dict = {"mode": mode, "messages": [], "init_tools": None, "error": None}
    HOOK_CALLS.clear()
    t0 = time.time()
    prompt = ("Search the web once for a newer edition of a synthetic book, then answer in one line."
              if websearch else "Reply with the word ok.")
    try:
        async for msg in query(prompt=prompt, options=opts):
            kind = type(msg).__name__
            entry = {"type": kind, "t": round(time.time() - t0, 3)}
            if kind == "AssistantMessage":
                entry["error"] = getattr(msg, "error", None)
                entry["model"] = getattr(msg, "model", None)
            if websearch and kind in ("AssistantMessage", "UserMessage"):
                content = getattr(msg, "content", None)
                entry["blocks"] = ([block_summary(b) for b in content] if isinstance(content, list)
                                   else str(content)[:300])
                if hasattr(msg, "tool_use_result"):
                    entry["tool_use_result"] = json.loads(json.dumps(getattr(msg, "tool_use_result"), default=str))
            if websearch and kind == "ResultMessage":
                entry["model_usage"] = getattr(msg, "model_usage", None)
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
    if websearch:
        out["hook_calls"] = list(HOOK_CALLS)
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
