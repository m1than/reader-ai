"""SRV-OBS FR-OBS-40, FR-OBS-24 [db]: the judge labelling queue and item reads over `judge_inputs` and
`obs_eval_scores` (both item kinds, B8 card views through the card-reader port, expiry, 409 cases, cascade of
sample scores), and judge labels: the record with slot and note, relabel revisions, `not_labelable`, and the
HUMAN mirror `owner.<slot>_ok` on the judge's trace with metadata that never carries the note.

@trace FR-OBS-40 FR-OBS-24 INV-OBS-6
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from .conftest import require
from .kl_db import CARDS, Request, engine_for, record_output, user

pytestmark = pytest.mark.anyio
NOTE = "NOTE-CANARY kept an unrelated card"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Cards:
    """A card-reader port: the owner's own pack is entitled, any other pack gives the MCP row (no L1 text)."""

    def __init__(self, entitled: set[str], resolvable: bool = True) -> None:
        self.entitled, self.ok = entitled, resolvable

    async def read(self, conn: Any, viewer: uuid.UUID, card_ids: list[str]) -> list[Any]:
        jl = require("reader.obs.judge_labels")
        return [jl.CardView(id=c, text=(f"text of {c}" if c in self.entitled else None),
                            view=("entitled" if c in self.entitled else "mcp")) for c in card_ids]

    async def resolvable(self, conn: Any, card_ids: list[str]) -> bool:
        return self.ok


async def _judge_input(engine: Any, caller: uuid.UUID, query: str = "q", sufficiency: float = 0.8) -> str:
    ji = require("reader.obs.judge_inputs")
    request = Request(caller, query, CARDS)
    output = {"keep": [CARDS[0][0]], "drop": [CARDS[1][0], CARDS[2][0]], "sufficiency": sufficiency}
    async with engine.begin() as conn:
        await record_output(conn, request, output)
        await ji.SINK.copy(conn, request, output)
        row = (await conn.execute(text("SELECT id FROM judge_inputs ORDER BY created_at DESC LIMIT 1"))).one()
        await conn.execute(text("UPDATE judge_inputs SET judge_trace_id = :t, route = 'exact', channel = 'tutor'"
                                " WHERE id = :id"), {"t": "ab" * 16, "id": row.id})
    return str(row.id)


async def _score(engine: Any, owner: uuid.UUID, slot: str, value: float, judge_input: str | None = None) -> str:
    sid = uuid.uuid4()
    async with engine.begin() as conn:
        eval_id = None
        if judge_input is None:
            eval_id = uuid.uuid4()
            doc = {"questions": [{"id": "e01", "question": "What is a hardened key?", "question_type": "exact"}]}
            await conn.execute(text(
                "INSERT INTO obs_eval_runs (id, owner_id, mode, set_name, set_doc, budget_profile, judges, records,"
                " deadline, state) VALUES (:id, :o, 'dataset', 'brain-eval-v1', CAST(:d AS jsonb), 'mcp',"
                " ARRAY[:s], 1, now() + interval '1 hour', 'done')"), {"id": eval_id, "o": owner,
                                                                       "d": json.dumps(doc), "s": slot})
            await conn.execute(text(
                "INSERT INTO obs_eval_records (eval_id, record_id, owner_id, card_ids, state)"
                " VALUES (:e, 'e01', :o, CAST(:c AS text[]), 'done')"), {"e": eval_id, "o": owner,
                                                                         "c": [c[0] for c in CARDS]})
        await conn.execute(text(
            "INSERT INTO obs_eval_scores (id, owner_id, eval_id, judge_input_id, record_id, slot, judge, value,"
            " trace_id) VALUES (:id, :o, :e, :ji, :r, :s, :j, :v, :t)"),
            {"id": sid, "o": owner, "e": eval_id, "ji": judge_input, "r": None if judge_input else "e01",
             "s": slot, "j": f"{slot}@{'c' * 12}", "v": value, "t": "cd" * 16})
    return str(sid)


async def test_queue_and_items_over_both_kinds(pg: Any, monkeypatch: Any) -> None:
    jl = require("reader.obs.judge_labels")
    engine = await engine_for(pg)
    try:
        owner = await user(engine, "owner")
        learner = await user(engine, "learner", consent=True)
        silent = await user(engine, "learner", consent=False)
        mine = await _judge_input(engine, owner, "owner question")
        theirs = await _judge_input(engine, learner, "learner question")
        await _judge_input(engine, silent, "never stored")  # not labelable: never queued
        rel = await _score(engine, owner, "judge.eval.relevance", 0.2, judge_input=mine)  # disagrees with keep
        dataset = await _score(engine, owner, "judge.eval.sufficiency", 0.55)
        monkeypatch.setattr(jl, "CARDS", Cards(entitled={CARDS[0][0]}))
        monkeypatch.setattr(jl, "THRESHOLDS", lambda: {"judge.eval.sufficiency": 0.5})
        async with engine.connect() as conn:
            find_q = await jl.queue(conn, owner, "judge.find", 25)
            assert [i["id"] for i in find_q] == [f"ji:{mine}", f"ji:{theirs}"]  # class 1 first
            suff_q = await jl.queue(conn, owner, "judge.eval.sufficiency", 25)
            assert [i["id"] for i in suff_q] == [f"es:{dataset}"]
            assert [i["id"] for i in await jl.queue(conn, owner, "judge.eval.relevance", 25)] == [f"es:{rel}"]
            got = await jl.item(conn, owner, f"ji:{theirs}")
            assert got["query"] == "learner question"
            assert [(c["id"], c["view"], c["text"]) for c in got["cards"]] == [
                (CARDS[0][0], "entitled", f"text of {CARDS[0][0]}"),
                (CARDS[1][0], "mcp", None), (CARDS[2][0], "mcp", None)]
            assert got["output"]["keep"] == [CARDS[0][0]]
            ds = await jl.item(conn, owner, f"es:{dataset}")
            assert ds["query"] == "What is a hardened key?" and ds["value"] == 0.55
            assert await jl.item(conn, owner, f"ji:{uuid.uuid4()}") is None
        monkeypatch.setattr(jl, "CARDS", Cards(entitled=set(), resolvable=False))
        async with engine.connect() as conn:
            with pytest.raises(jl.NotLabelable):
                await jl.item(conn, owner, f"es:{dataset}")
        async with engine.begin() as conn:  # expiry: the judge input goes, its sample score with it
            await conn.execute(text("DELETE FROM judge_inputs WHERE id = :i"), {"i": mine})
            assert (await conn.execute(text("SELECT count(*) FROM obs_eval_scores WHERE id = :i"),
                                       {"i": rel})).scalar_one() == 0
    finally:
        await engine.dispose()


async def test_judge_labels_record_mirror_and_refusals(pg: Any) -> None:
    labels = require("reader.obs.labels")
    engine = await engine_for(pg)
    try:
        owner = await user(engine, "owner")
        silent = await user(engine, "learner", consent=False)
        mine = await _judge_input(engine, owner)
        unlabelable = await _judge_input(engine, silent, "x")
        body = labels.LabelBody.model_validate({"target": "judge", "id": f"ji:{mine}", "ok": False, "note": NOTE})
        async with engine.begin() as conn:
            revision, label_id = await labels.record(conn, owner, body, mirror_on=True)
            assert revision == 1
            again = await labels.record(conn, owner, body, mirror_on=True)
            assert again is not None and again[0] == 2
            row = (await conn.execute(text("SELECT * FROM owner_labels WHERE id = :i"), {"i": label_id})).one()
            assert (row.target, row.slot, row.note, row.reason) == ("judge", "judge.find", NOTE, None)
            args = [r.args for r in (await conn.execute(text(
                "SELECT args FROM job_keys WHERE kind = 'obs.label'"))).all()]
            assert args and NOTE not in json.dumps(args)
            with pytest.raises(labels.NotLabelable):
                await labels.record(conn, owner, labels.LabelBody.model_validate(
                    {"target": "judge", "id": f"ji:{unlabelable}", "ok": True}), mirror_on=False)
            assert await labels.record(conn, owner, labels.LabelBody.model_validate(
                {"target": "judge", "id": f"ji:{uuid.uuid4()}", "ok": True}), mirror_on=False) is None
            row = (await conn.execute(text(labels.ROW_SQL), {"id": label_id})).one()
            ref = await labels.judge_trace_ref(conn, row)
        assert ref == "tr-" + "ab" * 16
        posted: list[dict[str, Any]] = []

        def reply(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json={"trace": {"trace_info": {"assessments": []}}})
            posted.append(json.loads(request.content))
            return httpx.Response(200, json={"assessment": {"assessment_id": "a-1"}})

        label = labels.Label(target="judge", target_id=mine, ok=False, reason=None, revision=2, assessment_id=None,
                             slot="judge.find")
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply), base_url="http://mlflow:5000") as c:
            got = await labels.mirror(label, job_revision=2, trace_ref=ref, client=c)
        assert got == "a-1"
        (sent,) = posted
        assert sent["assessment"]["assessment_name"] == "owner.judge_find_ok"
        assert sent["assessment"]["source"] == {"source_type": "HUMAN", "source_id": "owner"}
        assert sent["assessment"]["metadata"] == {"judge_input_id": mine, "revision": "2"}
        assert NOTE not in json.dumps(sent)
    finally:
        await engine.dispose()
