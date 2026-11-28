"""SRV-TUTOR [db], ADR-0039 revision (V.kl-tutor-find): the `tutor.answer` handler retrieves through SRV-SEARCH's
real `find` on channel `tutor` over `StoreCorpus` scoped to the course's material (a fake embed service, a fake
Provider): the call's shape and its one (policy, version) pair, the material and owner scope, failures before any
model call, degraded retrieval, the byte budget, citations carrying the stored quote hash on an r2 and an r3 pack,
the judge left out under a judging test policy, and the canary through the real path.

@trace FR-TUTOR-5 FR-TUTOR-8 INV-TUTOR-2 INV-TUTOR-4
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from ingest.packseed import R3, add_pack
from obs.conftest import finished, obs_setup, require
from search.conftest import EmbedServer, embed_args, embed_server, run_embed

from .conftest import (
    MAT,
    SPANS,
    FakeFind,
    FakeJobs,
    FakeProvider,
    Pg,
    Reply,
    World,
    add_material,
    answer_json,
    client,
    deps,
    insert_question,
    locators,
    need,
    policy_pair,
    question_row,
    rows,
    run_handler,
    scalar,
    world,
)

pytestmark = pytest.mark.anyio
__all__ = ["embed_server"]  # the fixture, imported for pytest
OTHER = "e" * 64
QUESTION = "Why do miners vary the nonce?"


@pytest.fixture
def embed(monkeypatch: pytest.MonkeyPatch, embed_server: EmbedServer) -> EmbedServer:
    monkeypatch.setenv("READER_EMBED_URL", embed_server.url)
    return embed_server


async def _embed(w: World, handle: str, sha: str = MAT) -> None:
    """Build the material's span-vector overlay for its active pack (SRV-SEARCH FR-SEARCH-5)."""
    await run_embed(w, await embed_args(w, w.users[handle], sha))  # type: ignore[arg-type]


def _real_find() -> Any:
    return need("reader.search.find").find


def _sent(provider: FakeProvider) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = json.loads(provider.calls[0].prompt)["sources"]
    return sources


# ---- FR-TUTOR-5, -8: end to end over the real find


async def test_real_find_over_an_r2_then_an_r3_pack_cites_the_stored_hash(
    pg: Pg, tmp_path: Path, embed: EmbedServer
) -> None:
    w = await world(pg, tmp_path)
    await _embed(w, "ivan")
    qid = await insert_question(w, question=QUESTION)
    provider = FakeProvider()
    await run_handler(w, qid, deps(w, provider, _real_find()))
    state, stored, _, code, _ = await question_row(w, qid)
    assert (state, code, stored["retrieval"]) == ("answered", None, "ok")
    assert {s["text"] for s in _sent(provider)} <= {text for _, text in SPANS}
    async with w.engine.connect() as conn:
        r2_ids = await locators(conn, w.users["ivan"], MAT)
    hashes = dict(await rows(w.engine, "SELECT locator, quote_sha256 FROM material_spans WHERE owner_id = :o",
                             o=w.users["ivan"]))
    (citation,) = stored["blocks"][0]["citations"]
    assert citation["span"] in r2_ids.values()
    assert citation["quote_sha256"] == hashes[citation["span"]]  # the r2 pack's backfilled hash

    r3_spans = [(61, "text", "Re-converted: miners vary the nonce until the hash falls below the target."),
                (62, "text", "Re-converted: the target moves every 2016 blocks.")]
    async with w.engine.begin() as conn:
        r2_ingest = (await need("reader.ingest.views").active_pack(conn, w.users["ivan"], MAT)).ingest_id
        r3 = await add_pack(conn, w.users["ivan"], MAT, R3, r3_spans, activate=True)
        # FR-INGEST-39: an r3 pack's overlays activate only once the legacy pack's seq map is accepted
        await conn.execute(text(
            "INSERT INTO seq_maps (owner_id, from_ingest_id, to_pack_id, state, span_count)"
            " VALUES (:o, :i, :p, 'accepted', 3)"), {"o": w.users["ivan"], "i": r2_ingest, "p": r3})
    await _embed(w, "ivan")
    blocks = [{"text": "x", "spans": ["s1"], "quote": "miners vary the nonce until the hash"}]
    provider = FakeProvider([Reply(text=answer_json(blocks))])
    qid = await insert_question(w, question=QUESTION)
    await run_handler(w, qid, deps(w, provider, _real_find()))
    state, stored, _, code, _ = await question_row(w, qid)
    assert (state, code) == ("answered", None)
    assert {s["text"] for s in _sent(provider)} <= {t for _, _, t in r3_spans}  # the active r3 pack only
    async with w.engine.connect() as conn:
        r3_ids = await locators(conn, w.users["ivan"], MAT)
        rows3 = await need("reader.ingest.views").active_span_rows(conn, w.users["ivan"], list(r3_ids.values()))
    (citation,) = stored["blocks"][0]["citations"]
    assert citation["span"] in r3_ids.values()
    assert citation["quote_sha256"] == rows3[citation["span"]][2]
    assert citation["quote_sha256"] == need("reader.ingest.locator").quote_sha256(rows3[citation["span"]][1])


async def test_the_find_call_and_its_one_policy_pair(pg: Pg, tmp_path: Path, embed: EmbedServer) -> None:
    w = await world(pg, tmp_path)
    await _embed(w, "ivan")
    real = _real_find()
    calls: list[dict[str, Any]] = []
    pairs: list[tuple[Any, str]] = []

    async def spy(caller: Any, channel: str, query: str, **kw: Any) -> Any:
        calls.append({"caller": caller, "channel": channel, "query": query, **kw})
        return await real(caller, channel, query, **kw)

    def policy() -> tuple[Any, str]:
        pairs.append(policy_pair())
        return pairs[-1]

    qid = await insert_question(w, question=QUESTION)
    await run_handler(w, qid, deps(w, FakeProvider(), spy, policy=policy))
    assert (await question_row(w, qid))[0] == "answered"
    (call,) = calls
    assert len(pairs) == 1
    loaded, version = pairs[0]
    assert set(call) == {"caller", "channel", "query", "deps"}  # no budget, k, filters or judge_ctx
    assert (call["channel"], call["query"]) == ("tutor", QUESTION)
    assert (call["caller"].user_id, call["caller"].roles) == (w.users["ivan"], ("owner",))
    found = call["deps"]
    assert found.judge is None
    assert found.policy is loaded and found.corpus.policy is loaded
    assert found.policy_version() == version
    assert found.corpus.material_sha256 == MAT


async def test_other_materials_and_other_owners_are_never_sent(
    pg: Pg, tmp_path: Path, embed: EmbedServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    anna = [(page, f"ANNA-ONLY {text}") for page, text in SPANS]
    w = await world(pg, tmp_path, {"anna": anna})
    # ivan's second material holds the question itself: the nearest span of all, outside the course's material.
    await add_material(w, "ivan", OTHER, [(401, f"OTHER-MATERIAL {QUESTION}")])
    for handle, sha in (("ivan", MAT), ("ivan", OTHER), ("anna", MAT)):
        await _embed(w, handle, sha)
    opening = need("reader.search.opening")
    real_load = opening.load_book
    loaded: list[str] = []

    async def spy(conn: Any, pack: Any, policy: Any) -> Any:
        loaded.append(pack.material_sha256)
        return await real_load(conn, pack, policy)

    monkeypatch.setattr(opening, "load_book", spy)
    provider = FakeProvider()
    qid = await insert_question(w, question=QUESTION)
    await run_handler(w, qid, deps(w, provider, _real_find()))
    assert (await question_row(w, qid))[0] == "answered"
    texts = [s["text"] for s in _sent(provider)]
    assert texts and all(not t.startswith(("OTHER-MATERIAL", "ANNA-ONLY")) for t in texts)
    assert set(loaded) == {MAT}  # the scoped corpus never loads the other material's Book


# ---- FR-TUTOR-5: failures before any model call, degraded retrieval, the byte budget


async def test_policy_and_find_errors_are_tutor_failed_without_a_call(pg: Pg, tmp_path: Path) -> None:
    from reader.search.find import FindError
    from reader.search.policy import PolicyError

    w = await world(pg, tmp_path)

    def broken_policy() -> tuple[Any, str]:
        raise PolicyError("config_unavailable", "test")

    async def broken_find(*_args: Any, **_kw: Any) -> Any:
        raise FindError("policy_invalid")

    provider, find = FakeProvider(), FakeFind()
    qid = await insert_question(w, question="Policy?")
    await run_handler(w, qid, deps(w, provider, find, policy=broken_policy))  # PolicyError from current()
    assert (await question_row(w, qid))[0::3] == ("failed", "tutor_failed")
    assert find.calls == []
    qid = await insert_question(w, question="Find?")
    await run_handler(w, qid, deps(w, provider, broken_find))  # FindError(policy_invalid) from find
    assert (await question_row(w, qid))[0::3] == ("failed", "tutor_failed")
    assert provider.calls == []


async def test_embed_down_is_degraded_retrieval(
    pg: Pg, tmp_path: Path, embed: EmbedServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = await world(pg, tmp_path)
    await _embed(w, "ivan")
    monkeypatch.setenv("READER_EMBED_URL", "http://127.0.0.1:9")  # nothing listens: the full-text list serves
    qid = await insert_question(w, question=QUESTION)
    provider = FakeProvider()
    await run_handler(w, qid, deps(w, provider, _real_find()))
    state, stored, _, _, _ = await question_row(w, qid)
    assert (state, stored["retrieval"]) == ("answered", "degraded")


async def test_bytes_drop_neighbours_before_the_last_primary(pg: Pg, tmp_path: Path) -> None:
    params = need("reader.tutor.params")
    big = [(page, f"p{page} " + "nonce " * 330) for page in range(1, 17)]  # 16 spans of about 1,985 bytes
    w = await world(pg, tmp_path, {"ivan": big})
    cards = [(seq, seq + 1) for seq in range(0, 16, 2)]  # 8 primaries (even seqs), each with its next (odd)
    provider = FakeProvider()
    qid = await insert_question(w, question="Nonce?")
    await run_handler(w, qid, deps(w, provider, FakeFind(cards=cards)))
    sent = [s["page"] for s in _sent(provider)]
    primaries = [seq + 1 for seq in range(0, 16, 2)]  # pages of the primaries, in response order
    assert sent[:8] == primaries  # every primary, the last one included
    assert 8 < len(sent) < 16  # the bytes bind: the last neighbours are dropped
    assert sent[8:] == [p + 1 for p in primaries][: len(sent) - 8]
    assert len(provider.calls[0].prompt.encode()) <= params.PROMPT_BYTES_MAX


# ---- FR-TUTOR-5, -8: legacy ids never reach the loader


async def test_legacy_ids_from_find_never_reach_the_loader(
    pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = need("reader.tutor.store")
    w = await world(pg, tmp_path)
    async with w.engine.connect() as conn:
        ids = await locators(conn, w.users["ivan"], MAT)
    legacy = f"{MAT}:0"
    raw = [{"id": legacy, "layer": "L1", "next": [ids[1]]}, {"id": ids[2], "layer": "L1", "next": [f"{MAT}:1"]}]
    real = store.spans_by_ids
    asked: list[list[str]] = []

    async def spy(conn: Any, owner: uuid.UUID, span_ids: Any) -> Any:
        asked.append(list(span_ids))
        return await real(conn, owner, span_ids)

    monkeypatch.setattr(store, "spans_by_ids", spy)
    blocks = [{"text": "x", "spans": ["s1", "s2"], "quote": "The target is adjusted every 2016 blocks"}]
    provider = FakeProvider([Reply(text=answer_json(blocks))])
    qid = await insert_question(w)
    await run_handler(w, qid, deps(w, provider, FakeFind(raw=raw)))
    assert asked == [[ids[2], ids[1]]]  # the loader never receives a legacy id
    stored = (await question_row(w, qid))[1]
    citations = [c for b in stored["blocks"] for c in b["citations"]]
    assert [c["span"] for c in citations] == [ids[2], ids[1]]
    assert all(c["span"].startswith("spn:") and len(c["quote_sha256"]) == 64 for c in citations)


async def test_a_long_span_is_cited_with_its_stored_hash(pg: Pg, tmp_path: Path) -> None:
    """FR-TUTOR-8, INV-TUTOR-2 (code review round 1): a span over span_bytes_max is cut when sent; its citation
    carries material_spans.quote_sha256, never a hash computed from the sent text."""
    params = need("reader.tutor.params")
    long_text = SPANS[0][1] + " filler" * 400  # about 2,900 bytes
    assert len(long_text.encode()) > params.SPAN_BYTES_MAX
    w = await world(pg, tmp_path, {"ivan": [(61, long_text), *SPANS[1:]]})
    provider = FakeProvider()
    qid = await insert_question(w)
    await run_handler(w, qid, deps(w, provider, FakeFind(cards=[(0, None)])))
    (sent,) = _sent(provider)
    assert len(sent["text"].encode()) <= params.SPAN_BYTES_MAX
    stored = (await question_row(w, qid))[1]
    (citation,) = stored["blocks"][0]["citations"]
    assert citation["quote_sha256"] == await scalar(
        w.engine, "SELECT quote_sha256 FROM material_spans WHERE owner_id = :o AND locator = :l",
        o=w.users["ivan"], l=citation["span"])


async def test_the_cards_own_text_and_title_are_never_sent(pg: Pg, tmp_path: Path) -> None:
    """FR-TUTOR-5 (code review round 1): the sent text is the stored span's, never the card's text or title."""
    w = await world(pg, tmp_path)
    async with w.engine.connect() as conn:
        ids = await locators(conn, w.users["ivan"], MAT)
    raw = [{"id": ids[0], "layer": "L1", "title": "CARD-TITLE", "text": "CARD-TEXT", "next": []}]
    provider = FakeProvider()
    qid = await insert_question(w)
    await run_handler(w, qid, deps(w, provider, FakeFind(raw=raw)))
    (sent,) = _sent(provider)
    assert sent["text"] == SPANS[0][1]
    assert "CARD-TEXT" not in provider.calls[0].prompt and "CARD-TITLE" not in provider.calls[0].prompt


# ---- the judge stays out (kl_open_points 2)


async def test_a_judging_test_policy_without_a_judge_spends_nothing(
    pg: Pg, tmp_path: Path, embed: EmbedServer
) -> None:
    policy_mod = need("reader.search.policy")
    data = json.loads(policy_mod.POLICY_FILE.read_text(encoding="utf-8"))
    data["gate"] = {"mode": "always_judge", "margin": None}
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    judging = policy_mod.load(path)
    w = await world(pg, tmp_path)
    await _embed(w, "ivan")
    qid = await insert_question(w, question=QUESTION)
    provider = FakeProvider()
    pair = (judging, policy_mod.policy_version(judging, slots={}))
    await run_handler(w, qid, deps(w, provider, _real_find(), policy=lambda: pair))
    state, stored, _, _, _ = await question_row(w, qid)
    assert (state, stored["retrieval"]) == ("answered", "degraded")  # FR-SEARCH-29 skip `error`: no judge
    assert await scalar(w.engine, "SELECT count(*) FROM judge_requests") == 0
    assert await scalar(w.engine, "SELECT count(*) FROM agent_spend") == 0  # $0 on every line (fake provider)


# ---- INV-TUTOR-4 through the real find


class _Captured:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict[str, Any]]] = []

    async def enqueue(self, kind: str, args: Any, schedule_in: int = 0) -> Any:
        self.enqueued.append((kind, dict(args)))
        return None


async def test_canary_through_the_real_find_reaches_no_log_span_or_job_arg(
    pg: Pg, tmp_path: Path, embed: EmbedServer, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import reader.jobs
    from reader.jobs.model import JobContext

    canaries = {"q": "CANARY-QUESTION-3e1f", "span": "CANARY-SPAN-c0d9"}
    marked = [(page, f"{text} {canaries['span']}") for page, text in SPANS]
    w = await world(pg, tmp_path, {"ivan": marked, "anna": marked})
    await _embed(w, "ivan")
    captured = _Captured()
    monkeypatch.setattr(reader.jobs, "running", lambda: captured)
    jobs = FakeJobs()
    with caplog.at_level(logging.DEBUG):
        async with client(w, jobs=jobs) as c:
            r = await c.post(f"/api/lessons/{w.lessons['ivan']}/ask",
                             json={"question": f"Why do miners vary the nonce {canaries['q']}?"})
            qid = uuid.UUID(r.json()["question_id"])
        kind = need("reader.tutor.jobs").make_kind(deps(w, FakeProvider(), _real_find()))
        args = kind.args.model_validate({"question_id": str(qid)})
        with obs_setup("worker") as (handle, memory):
            tracer = require("reader.obs").tracer()
            with tracer.start_as_current_span("job tutor.answer"):
                async with w.engine.connect() as conn, conn.begin():
                    await kind.handler(args, conn, JobContext(job_id=7, key="k", attempts=0))
            spans = finished(handle, memory)
    assert (await question_row(w, qid))[0] == "answered"
    assert any(s.name.startswith("find") for s in spans)  # the real find's text-free subtree ran
    logged = "\n".join(r.getMessage() for r in caplog.records)
    attributes = json.dumps([dict(s.attributes or {}) for s in spans], default=str)
    for canary in canaries.values():
        assert canary not in logged
        assert canary not in attributes
        assert canary not in json.dumps(jobs.enqueued) + json.dumps(captured.enqueued, default=str)
