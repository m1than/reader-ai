"""SRV-EVENTS [db]: no query or card text in the queue (INV-EVENTS-8, ADR-0039 B7b): a canary request through
`put` + `enqueue` + a judge-lane run of SRV-AGENT's handler (a provider that fails) never reaches the job
tables or a log record, and lives in `judge_requests` only until the run ends.

@trace INV-EVENTS-8
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

import pytest

from .conftest import Pg
from .test_judge_requests_db import CARDS, _db_now, _lane_env, _new, _put, _user, _work_judge_queue

pytestmark = pytest.mark.anyio
QUERY = "zqcanaryquery hardened derivation"
CARD_TEXTS = ("zqcanarycard-alpha text", "zqcanarycard-beta text")


async def test_canary_text_never_reaches_the_queue_or_the_logs(
    pg: Pg, caplog: pytest.LogCaptureFixture
) -> None:
    from reader.agent import judges

    caplog.set_level(logging.DEBUG)
    async with _lane_env(pg) as (env, _kind):
        owner = await _user(env)
        cards = tuple((card_id, text) for (card_id, _), text in zip(CARDS, CARD_TEXTS, strict=True))
        sha = judges.find_input_sha256(owner, QUERY, list(cards))
        deadline = await _db_now(env) + timedelta(seconds=5)
        new = _new(owner, deadline, query=QUERY, cards=cards, input_sha256=sha)
        await _put(env, new)
        stored = await env.scalar("SELECT query FROM judge_requests")
        assert stored == QUERY  # the row is the only place the text lives
        await env.jobs.enqueue("judge.find", {"request_id": str(new.request_id), "input_sha256": sha})
        args = await env.rows("SELECT args::text FROM procrastinate_jobs WHERE task_name = 'judge.find'")
        payload: Any = args[0][0]
        assert '"request_id"' in payload
        assert '"input_sha256"' in payload
        await _work_judge_queue(env)
        canaries = (QUERY, *CARD_TEXTS)
        dumps = [
            await env.rows("SELECT args::text FROM procrastinate_jobs"),
            await env.rows("SELECT args::text, key FROM job_keys"),
            await env.rows("SELECT row_to_json(e)::text FROM procrastinate_events e"),
        ]
        blob = " ".join(str(d) for d in dumps) + "\n" + caplog.text
        for canary in canaries:
            for start in range(0, len(canary) - 7):
                assert canary[start : start + 8] not in blob, canary[start : start + 8]
        assert await env.scalar("SELECT count(*) FROM judge_requests") == 0  # deleted at the run's end
        key_args = await env.rows("SELECT args::text FROM job_keys WHERE kind = 'judge.find'")
        assert set(json.loads(key_args[0][0])) == {"request_id", "input_sha256"}
