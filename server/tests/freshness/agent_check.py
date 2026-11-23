"""Run inside reader-server:local by test_agent_image.py (not collected: no `test_` prefix).

INV-FRESH-1 [agent]. In one process, with `--network none`, no token and no credit:
  - the handshake spike's stub on 127.0.0.1:8080 in its `websearch` scenario; with STUB_QUERY_FROM_PROMPT=1
    the main loop's tool_use carries the run's own `Query: <q>` line, so the gate sees the planned query;
  - a real agent-proxy (`reader.agent.proxy.create_app`) on 127.0.0.1:8090 with the stub as its upstream,
    through a transport that records every body the proxy forwards;
  - the real adapter (`reader.agent.provider.AgentProvider`) with the real bundled CLI (`sdk_cli`), wrapped
    only to record the SDK options it builds; the ledger's reserve and settle are replaced in memory (no
    database).
A synthetic material holds canary sentences in non-front spans; a fake scan output copies canary words into
subjects (5 words; 6 joined by punctuation; a 7-word copy with a topic word inside). The real `scan.parse` and
`queries.plan` run on it, and each planned query runs as `jobs.search_call` builds it, under a fresh `Gate`.
Prints one JSON line: the planned queries (synthetic), each run's hook matcher and identity, gate counts,
outcome and R size, and per forwarded body its kind, canary words and whether a 6-word canary run appears.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import subprocess
import sys
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from reader.agent import ledger
from reader.agent.errors import AgentError
from reader.agent.provider import AgentProvider, CliRequest, HttpProxyClient, StageCall, sdk_cli, sdk_options
from reader.agent.proxy import ProxySettings, create_app
from reader.config import load_config, secret_file_name
from reader.freshness import jobs, links, prompts, queries, sample, scan
from reader.freshness.gate import Gate
from reader.freshness.text import words

PROXY = "http://127.0.0.1:8090"
TOKEN_REF = "secret://keychain/reader-ai/claude-oauth-token"  # noqa: S105 (a reference; the value is synthetic)
WORK = Path("/tmp/fresh-check")  # noqa: S108 (the container's own tmpfs)
MAT = "c" * 64
TODAY = date(2026, 11, 24)
CONFIG = """\
version: 1
instance: { url: "https://localhost", data_dir: /tmp/data }
users:
  - handle: ivan
    role: owner
    providers:
      - { kind: anthropic-subscription, token: "secret://keychain/reader-ai/claude-oauth-token" }
tasks:
  freshness_web: { model: haiku }
sharing: {}
"""
# Canary sentences (synthetic, ≥ 6 unique words each); never in the outline or the bibliography.
S1 = "canaryaa canaryab canaryac canaryad canaryae canaryaf canaryag"
S2 = "canaryba canarybb canarybc canarybd canarybe canarybf"
S3 = "canaryca canarycb canarycc Ledgers canarycd latest version"  # a topic word and the template's words
S4 = "canaryda canarydb canarydc canarydd canaryde canarydf"
SENTENCES = (S1, S2, S3, S4)
SPANS = (
    sample.SpanRow(0, 1, "heading", "Practical Ledgers"),
    sample.SpanRow(1, 2, "text", "Example Press, 2019."),
    sample.SpanRow(2, 10, "text", f"Intro. {S1}. Outro."),
    sample.SpanRow(3, 11, "text", f"Intro. {S2}. Outro."),
    sample.SpanRow(4, 12, "text", f"Intro. {S3}. Outro."),
    sample.SpanRow(5, 13, "text", f"Intro. {S4}. Outro."),
)
OUTLINE = (sample.OutlineRow(0, 1, "Ledgers", 7), sample.OutlineRow(1, 1, "Blocks", 9))
BIB = {"title": "Practical Ledgers", "authors": ["Ada Example"], "year": 2019}


def scan_output(aliases: dict[str, Any]) -> str:
    alias = {span.seq: a for a, span in aliases.items()}

    def claim(seq: int, subject: str, kind: str, confidence: str) -> dict[str, str]:
        return {"span": alias[seq], "subject": subject, "kind": kind, "why": "w", "confidence": confidence}

    claims = [
        claim(2, "canaryaa canaryab canaryac canaryad canaryae", "version", "high"),  # 5 words: kept
        claim(3, "canaryba-canarybb-canarybc-canarybd-canarybe-canarybf", "tool", "high"),  # 6: dropped
        claim(4, "canaryca canarycb canarycc Ledgers canarycd", "version", "high"),  # a 7-word copy
        claim(5, "canaryda/canarydb", "api", "medium"),  # 2 words: kept
    ]
    return json.dumps({"bibliography": {"title": BIB["title"]}, "claims": claims})


class Recording(httpx.AsyncBaseTransport):
    """agent-proxy's upstream transport: records every body it forwards, then sends it to the stub."""

    def __init__(self) -> None:
        self.inner = httpx.AsyncHTTPTransport()
        self.bodies: list[bytes] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(await request.aread())
        return await self.inner.handle_async_request(request)


def prepare() -> Path:
    """reader.yaml and the synthetic provider token, as OPS-CLI delivers them (0700 directory, 0600 file)."""
    secrets_root = WORK / "secrets"
    secrets_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = secrets_root / secret_file_name(TOKEN_REF)
    token.write_text("sk-ant-oat01-SYNTHETIC-" + "x" * 40, encoding="utf-8")
    token.chmod(0o600)
    config = WORK / "reader.yaml"
    config.write_text(CONFIG, encoding="utf-8")
    return config


def body_view(body: bytes, runs: set[tuple[str, ...]], canary: set[str]) -> dict[str, Any]:
    payload = json.loads(body)
    tools = payload.get("tools") or []
    kind = "search" if any("type" in t for t in tools) else "main" if tools else "other"
    tokens = words(json.dumps(payload, ensure_ascii=False))
    return {
        "kind": kind,
        "canary_words": sorted({t for t in tokens if t in canary}),
        "canary_6run": any(tuple(tokens[i : i + 6]) in runs for i in range(len(tokens) - 5)),
    }


async def main(config_path: Path) -> dict[str, Any]:
    config = load_config(config_path)
    reserves: list[Decimal] = []

    async def reserve(*args: Any, **kwargs: Any) -> int:
        reserves.append(args[3])
        return len(reserves)

    async def settle(*args: Any, **kwargs: Any) -> None:
        return None

    ledger.reserve = reserve  # type: ignore[assignment]  # no database in this container
    ledger.settle = settle  # type: ignore[assignment]

    recording = Recording()
    settings = ProxySettings(
        config_path=config_path, secrets_root=WORK / "secrets", upstream="http://127.0.0.1:8080"
    )
    app = create_app(settings, upstream_transport=recording)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=8090, log_config=None, access_log=False, lifespan="off")
    )
    serving = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110 (uvicorn exposes a flag, not an event)
        await asyncio.sleep(0.05)

    built = sample.build(SPANS, OUTLINE, 20, "en")
    _, aliases = scan.render(built)
    scanned = scan.parse(scan_output(aliases), aliases, MAT, TODAY)
    plan = queries.plan(BIB, sample.topics(OUTLINE), scanned.claims, "en", TODAY, built.non_front_texts)

    options: list[Any] = []

    def cli(request: CliRequest) -> Any:
        options.append(sdk_options(request))
        return sdk_cli(request)

    proxy = HttpProxyClient(base_url=PROXY)
    home = WORK / "homes"
    home.mkdir(exist_ok=True)
    engine: Any = None  # the ledger is in memory above
    provider = AgentProvider(engine, config, proxy, cli=cli, home_root=home, proxy_url=PROXY)
    base = functools.partial(
        StageCall, owner_id=uuid.uuid4(), handle="ivan", run_id=uuid.uuid4(), lesson_id=None,
        run_cap_usd=Decimal("1.00"), daily_budget_usd=Decimal("2.00"), thinking_off=True,
    )
    prompt = prompts.load("search")
    canary = {w for s in SENTENCES for w in words(s) if w.startswith("canary")}
    shingles = queries.shingles(list(SENTENCES))
    out_runs: list[dict[str, Any]] = []
    for index, query in enumerate(plan.queries):
        gate = Gate(query.text)
        first = len(recording.bodies)
        run: dict[str, Any] = {"template": query.template}
        try:
            result = await provider.run(jobs.search_call(base, index, prompt, query.text, gate))
            run["outcome"] = "ok"
            run["r"] = len(links.collect(result.tool_results, 1).entries)
            run["web_search_requests"] = result.usage.web_search_requests
        except AgentError as error:
            run["outcome"] = error.code
        matchers = ((options[-1].hooks if options else None) or {}).get("PreToolUse", [])
        run["matchers"] = [m.matcher for m in matchers]
        one = len(matchers) == 1 and len(matchers[0].hooks) == 1
        run["hook_is_the_gate"] = one and matchers[0].hooks[0] is gate
        run["allowed"], run["denied"] = gate.allowed, gate.denied
        run["bodies"] = [body_view(b, shingles, canary) for b in recording.bodies[first:]]
        out_runs.append(run)

    await proxy.aclose()
    server.should_exit = True
    await serving
    return {
        "scan_dropped": dict(scanned.dropped),
        "plan_dropped": dict(plan.dropped),
        "queries": [[q.template, q.text] for q in plan.queries],
        "canary_words": sorted(canary),
        "sentence_6runs": [list(r) for r in sorted(shingles)],
        "runs": out_runs,
        "reserves": [str(r) for r in reserves],
    }


if __name__ == "__main__":
    env = {**os.environ, "STUB_SCENARIO": "websearch", "STUB_QUERY_FROM_PROMPT": "1"}
    pipe = subprocess.PIPE
    stub = subprocess.Popen([sys.executable, "/stub/stub.py"], stdin=pipe, stdout=pipe, env=env)
    assert stub.stdout is not None
    stub.stdout.readline()  # stub-ready
    sys.stdout.write(json.dumps(asyncio.run(main(prepare()))) + "\n")
