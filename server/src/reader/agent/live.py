"""`python -I -m reader.agent.live`: FR-AGENT-11's live call, run in the worker container by the live check.

One `fact_check`-task call (Sonnet in the example config) with a one-word answer through agent-proxy,
capped at $0.50; prints a JSON report (model, answered, ledger kinds, settled amount). Never prints a token
or the answer.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text

from reader.agent.provider import AgentProvider, HttpProxyClient, StageCall
from reader.config import load_config
from reader.db import make_engine
from reader.serve.main import Env, _target


async def _run() -> dict[str, object]:
    env = Env.read(os.environ)
    config = load_config(Path(env.config))
    owner = next(u for u in config.users if u.role == "owner")
    engine = make_engine(_target(env))
    try:
        async with engine.connect() as conn:
            owner_id = (
                await conn.execute(text("SELECT id FROM users WHERE handle = :h"), {"h": owner.handle})
            ).scalar_one()
        run = uuid.uuid4()
        provider = AgentProvider(engine, config, HttpProxyClient())
        result = await provider.run(
            StageCall(
                owner_id=owner_id,
                handle=owner.handle,
                run_id=run,
                lesson_id=None,
                stage="live_check",
                task="fact_check",
                attempt=0,
                system="Answer with exactly one word.",
                prompt="Reply with the word ok.",
                max_output_tokens=64,
                floor_usd=Decimal(0),
                run_cap_usd=Decimal("0.50"),
            )
        )
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text("SELECT kind, settled_usd FROM agent_spend WHERE run_id = :r ORDER BY id"),
                    {"r": run},
                )
            ).all()
        return {
            "model": result.model,
            "answered": "ok" in result.text.lower(),
            "kinds": [r[0] for r in rows],
            "settled_usd": float(next(r[1] for r in rows if r[0] == "settle")),
        }
    finally:
        await engine.dispose()


def main() -> int:
    sys.stdout.write(json.dumps(asyncio.run(_run())) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
