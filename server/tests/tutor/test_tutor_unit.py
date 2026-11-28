"""SRV-TUTOR [unit]: bounds, the error catalogue and its outcomes, answer validation, grounding, injection, prompt
sizing, the system prompt, cost derivation, the state map and the DoD hook.

@trace FR-TUTOR-3 FR-TUTOR-5 FR-TUTOR-6 FR-TUTOR-7 FR-TUTOR-8 FR-TUTOR-10 FR-TUTOR-13
@trace INV-TUTOR-2 INV-TUTOR-3
@trace VEC-TUTOR-1 VEC-TUTOR-2 VEC-TUTOR-3 VEC-TUTOR-4 VEC-TUTOR-5 VEC-TUTOR-9
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from .conftest import REPO, bounds, fixture, need, spec

D = Decimal
ANSWERS = fixture("answers.yaml")
INJECTION = fixture("injection.yaml")
COST = fixture("cost.yaml")
STATUS = fixture("status-map.yaml")["cases"]
CATALOGUE = fixture("errors.yaml")["codes"]
FIND_SOURCES = fixture("find-sources.yaml")
LEGACY = "a" * 64 + ":3"


def _sent() -> list[Any]:
    answer = need("reader.tutor.answer")
    return [
        answer.Source(alias, value["span"], value["page"], value["text"], value["quote_sha256"])
        for alias, value in ANSWERS["sent"].items()
    ]


def _citation(source: Any, title: str) -> dict[str, Any]:
    """FR-TUTOR-8 (revision): the sent span's id, page and stored hash, the material title; no hash beside a
    non-locator id."""
    out = {"span": source.span_id, "page": source.page, "material_title": title}
    if source.span_id.startswith("spn:"):
        out["quote_sha256"] = source.quote_sha256
    return out


# ---- bounds and the catalogue


def test_bounds_match_the_spec() -> None:
    params = need("reader.tutor.params")
    b = bounds()
    assert params.QUESTION_CHARS_MAX == b["question_chars_max"]
    assert params.QUESTION_BYTES_MAX == b["question_bytes_max"]
    # ADR-0039 revision: find on the tutor channel takes no k; SRV-SEARCH policy sets the response size.
    assert "retrieve_k" not in b
    assert not hasattr(params, "RETRIEVE_K")
    assert params.SPAN_BYTES_MAX == b["span_bytes_max"]
    assert params.CELL_BYTES_MAX == b["cell_bytes_max"]
    assert params.QUESTION_CELL_BYTES_MAX == b["question_cell_bytes_max"]
    assert params.QUESTION_CELL_BYTES_MAX == params.QUESTION_BYTES_MAX + params.CELL_BYTES_MAX + 1000
    assert params.SOURCES_MAX == b["sources_max"]
    assert params.PROMPT_BYTES_MAX == b["prompt_bytes_max"]
    assert params.SYSTEM_BYTES_MAX == b["system_bytes_max"]
    assert list(params.QUOTE_WORDS) == b["quote_words"]
    assert params.SPANS_PER_BLOCK_MAX == b["spans_per_block_max"]
    assert params.BLOCKS_MAX == b["blocks_max"]
    assert params.BLOCK_CHARS_MAX == b["block_chars_max"]
    assert params.MAX_OUTPUT_TOKENS == b["max_output_tokens"]
    assert params.CLI_OVERHEAD_TOKENS == b["cli_overhead_tokens"]
    assert params.ATTEMPTS_MAX == b["attempts_max"]
    assert D(str(b["question_cap_usd"])) == params.QUESTION_CAP_USD
    assert D(str(b["daily_cap_usd"])) == params.DAILY_CAP_USD
    assert params.QUESTION_TTL_S == b["question_ttl_s"]
    assert params.STALE_WITHOUT_JOB_S == b["stale_without_job_s"]
    assert params.CALL_DEADLINE_S == b["call_deadline_s"]
    assert params.JOB_TIMEOUT_S == b["job_timeout_s"]
    assert params.HISTORY_MAX == b["history_max"]
    assert params.PRIORITY == 100  # FR-TUTOR-3


def test_error_catalogue_matches_the_spec_and_the_web_messages() -> None:
    errors = need("reader.tutor.errors")
    assert list(errors.CODES) == spec()["errors"]
    assert list(CATALOGUE) == spec()["errors"]
    http = {code: v["http"] for code, v in CATALOGUE.items() if "http" in v}
    assert http == {
        "question_invalid": 400, "cell_unknown": 400, "unknown_revision": 400, "not_found": 404,
        "tutor_busy": 409, "daily_budget_exhausted": 429,
    }


@pytest.mark.parametrize(
    ("code", "state", "stored"),
    [
        ("budget_exhausted", "refused", "budget_exhausted"),
        ("daily_budget_exhausted", "refused", "daily_budget_exhausted"),
        ("monthly_budget_exhausted", "refused", "monthly_budget_exhausted"),
        ("task_budget_exhausted", "refused", "budget_exhausted"),
        ("provider_missing", "failed", "provider_missing"),
        ("upstream_retry", "failed", "provider_unavailable"),
        ("deadline_exceeded", "failed", "provider_unavailable"),
        ("rate_limited", "failed", "rate_limited"),
        ("provider_unavailable", "failed", "provider_unavailable"),
        ("output_invalid", "failed", "output_invalid"),
        ("output_truncated", "failed", "output_truncated"),
        ("sources_unavailable", "failed", "sources_unavailable"),
        ("tools_mismatch", "failed", "tutor_failed"),
        ("sdk_error", "failed", "tutor_failed"),
        ("unknown_model", "failed", "tutor_failed"),
        ("something_new", "failed", "tutor_failed"),
    ],
)
def test_outcome_of_every_code(code: str, state: str, stored: str) -> None:
    errors = need("reader.tutor.errors")
    assert errors.outcome(code) == (state, stored)


def test_transient_codes() -> None:
    errors = need("reader.tutor.errors")
    assert errors.TRANSIENT == frozenset(
        {"provider_unavailable", "rate_limited", "output_invalid", "upstream_retry", "deadline_exceeded"}
    )


# ---- VEC-TUTOR-1: answer validation


@pytest.mark.parametrize("case", ANSWERS["cases"], ids=lambda c: c["name"])
def test_answer_vectors(case: dict[str, Any]) -> None:
    answer = need("reader.tutor.answer")
    errors = need("reader.tutor.errors")
    raw = case.get("output_text") or json.dumps(case["output"], ensure_ascii=False)
    expect = case["expect"]
    if "error" in expect:
        with pytest.raises(errors.TutorTransient) as failure:
            answer.parse(raw)
        assert failure.value.code == expect["error"]
        return
    sent = _sent()
    checked = answer.check(answer.parse(raw), sent)
    assert [list(b.kept) for b in checked.blocks] == [b["kept"] for b in expect["blocks"]]
    assert [b.supported for b in checked.blocks] == [b["supported"] for b in expect["blocks"]]
    assert checked.grounded is expect["grounded"]
    title = ANSWERS["material_title"]
    by_alias = {s.alias: s for s in sent}
    for block, want in zip(checked.blocks, expect["blocks"], strict=True):
        cited = answer.citations(block.kept, sent, title)
        assert cited == [_citation(by_alias[a], title) for a in block.kept]  # every case follows the rule
        if "citations" in want:
            assert cited == want["citations"]
        assert all(set(c) == {"span", "page", "material_title", "quote_sha256"} for c in cited)


def test_parse_bounds() -> None:
    answer = need("reader.tutor.answer")
    errors = need("reader.tutor.errors")
    block = {"text": "x", "spans": ["s1"], "quote": ""}
    for bad in (
        {"blocks": [block] * 21, "insufficient": False},
        {"blocks": [{**block, "text": ""}], "insufficient": False},
        {"blocks": [{**block, "text": "x" * 4001}], "insufficient": False},
        {"blocks": [{**block, "spans": "s1"}], "insufficient": False},
        {"blocks": [{**block, "spans": [1]}], "insufficient": False},
        {"blocks": [block], "insufficient": "no"},
        {"blocks": [block]},
        [block],
    ):
        with pytest.raises(errors.TutorTransient):
            answer.parse(json.dumps(bad))
    assert len(answer.parse(json.dumps({"blocks": [block] * 20, "insufficient": False}))["blocks"]) == 20
    with pytest.raises(errors.TutorTransient):
        answer.parse("```json\n{}\n```\n```json\n{}\n```")


def test_quote_words_bound() -> None:
    answer = need("reader.tutor.answer")
    text = " ".join(f"w{i}" for i in range(40))
    source = answer.Source("s1", f"spn:{'a' * 64}:p1:0-10", 1, text)
    for n, ok in ((4, False), (5, True), (30, True), (31, False)):
        quote = " ".join(f"w{i}" for i in range(n))
        out = {"blocks": [{"text": "x", "spans": ["s1"], "quote": quote}], "insufficient": False}
        assert answer.check(out, [source]).blocks[0].supported is ok


# ---- INV-TUTOR-2: grounding by code, for any output

_ALIAS = st.sampled_from(["s1", "s2", "s3", "s4", "s9", "s0", "S1", "a" * 64 + ":1", "b" * 64 + ":7", "", "s1 "])
_HEX = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)
_BLOCK = st.fixed_dictionaries(
    {"text": st.one_of(st.text(min_size=1, max_size=50), _HEX), "spans": st.lists(_ALIAS, max_size=3),
     "quote": st.one_of(st.text(max_size=80), _HEX)}
)


@settings(max_examples=300, deadline=None)
@given(blocks=st.lists(_BLOCK, min_size=1, max_size=5), insufficient=st.booleans(), pick=st.integers(0, 40),
       legacy=st.booleans())
def test_grounding_property(blocks: list[dict[str, Any]], insufficient: bool, pick: int, legacy: bool) -> None:
    """INV-TUTOR-2 at the check-function level; the revision: a citation's hash is the sent span's, whatever the
    output holds, and over a sent set that mixes locators and a legacy id (as if FR-TUTOR-5's filter were
    bypassed) no citation carries quote_sha256 beside the legacy id."""
    answer = need("reader.tutor.answer")
    sent = _sent()
    if legacy:
        sent.append(answer.Source("s4", LEGACY, 70, "A legacy span text that a bypassed filter let through.", "f" * 64))
    by_alias = {s.alias: s for s in sent}
    # Sometimes plant a real excerpt so supported blocks occur.
    words = sent[0].text.split()
    if pick + 5 <= len(words):
        blocks[0] = {**blocks[0], "spans": ["s1"], "quote": " ".join(words[pick : pick + 6])}
    checked = answer.check(answer.parse(json.dumps({"blocks": blocks, "insufficient": insufficient})), sent)
    for block in checked.blocks:
        assert set(block.kept) <= set(by_alias)
        assert len(block.kept) == len(set(block.kept))
        if block.supported:
            first = by_alias[block.kept[0]]
            assert answer.normalize(block.quote) in answer.normalize(first.text)
            assert 5 <= len(answer.normalize(block.quote).split()) <= 30
        for citation, alias in zip(answer.citations(block.kept, sent, "T"), block.kept, strict=True):
            source = by_alias[alias]
            assert (citation["span"], citation["page"], citation["material_title"]) == (
                source.span_id, source.page, "T")
            if "quote_sha256" in citation:
                assert citation["span"].startswith("spn:")
                assert citation["quote_sha256"] == source.quote_sha256
            else:
                assert not source.span_id.startswith("spn:")
    assert checked.grounded == (not insufficient and all(b.supported for b in checked.blocks))


@given(st.text())
def test_parse_never_raises_anything_but_output_invalid(text: str) -> None:
    answer = need("reader.tutor.answer")
    errors = need("reader.tutor.errors")
    try:
        answer.parse(text)
    except errors.TutorTransient as failure:
        assert failure.code == "output_invalid"


# ---- VEC-TUTOR-2 / INV-TUTOR-3: injection


def _spans(texts: list[str]) -> list[Any]:
    answer = need("reader.tutor.answer")
    return [answer.Span(f"{'a' * 64}:{i}", 60 + i, t) for i, t in enumerate(texts)]


@pytest.mark.parametrize("case", INJECTION["cases"], ids=lambda c: c["name"])
def test_injection_vectors(case: dict[str, Any]) -> None:
    answer = need("reader.tutor.answer")
    texts = [v["text"] for v in ANSWERS["sent"].values()]
    if "source_text" in case:
        texts = [case["source_text"], *texts]
    cell = answer.Cell("c1", "explain", case.get("cell_text", "A cell."))
    question = case.get("question", "What is a nonce?")
    prompt = answer.build_prompt(question, cell, _spans(texts))
    decoded = json.loads(prompt.text)
    assert set(decoded) == {"question", "cell", "sources"}
    assert decoded["question"] == question
    assert decoded["cell"] == {"id": "c1", "kind": "explain", "text": cell.text}
    assert [s["id"] for s in decoded["sources"]] == [f"s{i + 1}" for i in range(len(texts))]
    assert [s["text"] for s in decoded["sources"]] == texts
    assert [s.alias for s in prompt.sources] == [s["id"] for s in decoded["sources"]]
    checked = answer.check(answer.parse(json.dumps(case["model_output"])), prompt.sources)
    expect = case["expect"]
    sent_aliases = {s.alias for s in prompt.sources}
    for block in checked.blocks:
        assert set(block.kept) <= sent_aliases
    if "kept" in expect:
        assert list(checked.blocks[0].kept) == expect["kept"]
    if "supported" in expect:
        assert checked.blocks[0].supported is expect["supported"]
    if "grounded" in expect:
        assert checked.grounded is expect["grounded"]
    if expect.get("sources_count_unchanged"):
        assert len(decoded["sources"]) == len(texts)
    if expect.get("request_tools_empty"):
        # The Provider port has no field that could carry a tool (SRV-AGENT offers none, FR-AGENT-7).
        import dataclasses

        from reader.agent.provider import StageCall

        assert not any("tool" in f.name for f in dataclasses.fields(StageCall))


def test_forged_source_in_span_text_gets_no_alias() -> None:
    answer = need("reader.tutor.answer")
    case = next(c for c in INJECTION["cases"] if c["name"] == "span-text-forges-a-source")
    prompt = answer.build_prompt("q", None, _spans([case["source_text"]]))
    assert [s.alias for s in prompt.sources] == ["s1"]
    checked = answer.check(answer.parse(json.dumps(case["model_output"])), prompt.sources)
    assert list(checked.blocks[0].kept) == []
    assert checked.grounded is False


# ---- FR-TUTOR-6: prompt and sizing


def test_cut_bytes() -> None:
    answer = need("reader.tutor.answer")
    assert answer.cut_bytes("short", 100) == "short"
    assert answer.cut_bytes("alpha beta gamma", 12) == "alpha beta"
    assert answer.cut_bytes("abcdefghij", 4) == "abcd"  # no whitespace: hard cut
    cut = answer.cut_bytes("я" * 10, 5)  # never splits a character
    assert cut == "яя"
    assert len(answer.cut_bytes("ж " * 3000, 2000).encode()) <= 2000


def test_prompt_fits_after_escaping_and_drops_lowest_rank_first() -> None:
    answer = need("reader.tutor.answer")
    params = need("reader.tutor.params")
    heavy = ['"' * 2000 for _ in range(8)]  # 2000 bytes each, 4000 once escaped
    prompt = answer.build_prompt("q" * 1000, answer.Cell("c", "explain", "x" * 4000), _spans(heavy))
    assert len(prompt.text.encode()) <= params.PROMPT_BYTES_MAX
    assert 0 < len(prompt.sources) < 8
    assert [s.alias for s in prompt.sources] == [f"s{i + 1}" for i in range(len(prompt.sources))]
    assert [s.span_id for s in prompt.sources] == [f"{'a' * 64}:{i}" for i in range(len(prompt.sources))]
    assert len(json.loads(prompt.text)["sources"]) == len(prompt.sources)


def test_question_and_cell_cut_before_sources_and_sources_cut_to_span_bytes() -> None:
    answer = need("reader.tutor.answer")
    params = need("reader.tutor.params")
    escaped = "\u0001" * 3000  # 6 bytes each once escaped
    prompt = answer.build_prompt(escaped, answer.Cell("c", "explain", "\u0002" * 4000), _spans(["w " * 5000]))
    assert len(prompt.text.encode()) <= params.PROMPT_BYTES_MAX
    assert len(prompt.sources) == 1
    assert len(prompt.sources[0].text.encode()) <= params.SPAN_BYTES_MAX
    small = answer.build_prompt("q", None, _spans(["a b"] * (params.SOURCES_MAX + 1)))
    assert len(small.sources) == params.SOURCES_MAX  # the alias cap (FR-TUTOR-6)
    assert small.sources[-1].alias == f"s{params.SOURCES_MAX}"
    assert json.loads(small.text)["cell"] is None


def test_no_source_is_sources_unavailable() -> None:
    answer = need("reader.tutor.answer")
    errors = need("reader.tutor.errors")
    with pytest.raises(errors.TutorError) as failure:
        answer.build_prompt("q", None, [])
    assert failure.value.code == "sources_unavailable"


# ---- FR-TUTOR-5 / -6: primaries before neighbours, so only bytes bind


def _loc(page: int) -> str:
    return f"spn:{'a' * 64}:p{page}:0-10"


def _card(card_id: str, nxt: list[str], layer: str = "L1") -> dict[str, Any]:
    return {"id": card_id, "layer": layer, "next": nxt}


@pytest.mark.parametrize("case", FIND_SOURCES["cases"], ids=lambda c: c["name"])
def test_find_sources_vectors(case: dict[str, Any]) -> None:
    """VEC-TUTOR-9: a find response's cards → the candidate span ids, in order."""
    jobs = need("reader.tutor.jobs")
    ids = FIND_SOURCES["ids"]
    cards = [_card(ids[c["id"]], [ids[n] for n in c["next"]], c["layer"]) for c in case["cards"]]
    assert jobs.candidate_ids(cards) == [ids[k] for k in case["expect"]]


def test_the_citation_hash_is_the_stored_one_never_computed() -> None:
    """FR-TUTOR-8, INV-TUTOR-2 (code review round 1): a span longer than span_bytes_max is cut when sent, and its
    citation still carries the stored hash, not a hash of the cut or the full text."""
    answer = need("reader.tutor.answer")
    params = need("reader.tutor.params")
    stored = "e" * 64
    prompt = answer.build_prompt("q", None, [answer.Span(_loc(1), 1, "w " * 1500, stored)])
    (source,) = prompt.sources
    assert len(source.text.encode()) <= params.SPAN_BYTES_MAX < len(("w " * 1500).encode())
    assert answer.citations(("s1",), prompt.sources, "T") == [
        {"span": _loc(1), "page": 1, "material_title": "T", "quote_sha256": stored}]


def test_legacy_ids_in_a_find_response_are_never_candidates() -> None:
    """FR-TUTOR-5/-8 (revision): a legacy `<sha>:<seq>` id as a card id or a next id is dropped before loading,
    so no alias can name it and no citation can carry quote_sha256 beside it."""
    jobs = need("reader.tutor.jobs")
    legacy_next = "b" * 64 + ":9"
    cards = [_card(LEGACY, [_loc(1)]), _card(_loc(2), [legacy_next, _loc(3)])]
    found = jobs.candidate_ids(cards)
    assert found == [_loc(2), _loc(1), _loc(3)]
    assert LEGACY not in found and legacy_next not in found


@pytest.mark.parametrize("span_text", ["a b c", "w " * 1000], ids=["small-spans", "bytes-bind"])
def test_every_primary_is_sent_before_any_neighbour(span_text: str) -> None:
    """FR-TUTOR-5/-6 (revision): 25 L1 cards, each with its next span; the primaries come first, so the byte
    budget drops neighbours of the lowest ranks before any primary."""
    jobs = need("reader.tutor.jobs")
    answer = need("reader.tutor.answer")
    params = need("reader.tutor.params")
    cards = [_card(_loc(2 * i + 1), [_loc(2 * i + 2), f"sec:{'a' * 64}:1"]) for i in range(25)]
    ordered = jobs.candidate_ids(cards)
    primaries = [c["id"] for c in cards]
    assert ordered[:25] == primaries  # every primary, in response order
    assert ordered[25:] == [c["next"][0] for c in cards]  # then each card's next span, in the same order
    spans = [answer.Span(s, 60, span_text, "e" * 64) for s in ordered[: params.SOURCES_MAX]]
    prompt = answer.build_prompt("q", None, spans)
    sent = [s.span_id for s in prompt.sources]
    if span_text == "a b c":
        assert sent == ordered  # small spans: neither the alias cap nor the bytes bind
    else:
        assert sent[:25] == primaries[: len(sent)]
        assert len(sent) < 50  # 50 x ~1,000 bytes > the sources budget: neighbours dropped first
        assert len(prompt.text.encode()) <= params.PROMPT_BYTES_MAX


# ---- FR-TUTOR-5 (revision): the judge pin and the import boundary


def test_shipped_gate_is_never_judge_while_the_tutor_has_no_judge() -> None:
    """kl_open_points 2: the tutor's default deps carry no judge exactly while the shipped gate never asks for one;
    the test fails when either changes alone."""
    jobs = need("reader.tutor.jobs")
    from reader.search import policy

    shipped = json.loads(policy.POLICY_FILE.read_text(encoding="utf-8"))
    never = shipped["gate"]["mode"] == "never_judge"
    assert never == (jobs.default_deps().judge is None)
    assert never and jobs.default_deps().judge is None  # this revision: both


def test_default_deps_reach_find_and_the_bound_policy() -> None:
    """FR-TUTOR-5 (revision): production retrieval is SRV-SEARCH's find with the process's bound (policy,
    version) pair."""
    jobs = need("reader.tutor.jobs")
    from reader.search import find, policy

    made = jobs.default_deps()
    assert made.find is find.find
    assert made.policy is policy.current


def test_no_tutor_module_imports_the_search_store() -> None:
    """FR-TUTOR-5 (revision): the tutor reaches SRV-SEARCH only through find, never `reader.search.store`."""
    root = REPO / "server" / "src" / "reader" / "tutor"
    pattern = re.compile(r"reader\.search\.store|from reader\.search import[^\n]*\bstore\b")
    offenders = [p.name for p in root.rglob("*.py") if pattern.search(p.read_text(encoding="utf-8"))]
    assert offenders == []


def test_prompt_bytes_max_is_inclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pinned from below: a prompt of exactly prompt_bytes_max keeps all its sources; one byte less drops one."""
    answer = need("reader.tutor.answer")
    params = need("reader.tutor.params")
    spans = _spans(["first source text", "second source text"])
    exact = len(answer.build_prompt("q", None, spans).text.encode())
    monkeypatch.setattr(params, "PROMPT_BYTES_MAX", exact)
    assert len(answer.build_prompt("q", None, spans).sources) == 2
    monkeypatch.setattr(params, "PROMPT_BYTES_MAX", exact - 1)
    assert [s.alias for s in answer.build_prompt("q", None, spans).sources] == ["s1"]


def test_system_prompt_file() -> None:
    answer = need("reader.tutor.answer")
    params = need("reader.tutor.params")
    path = REPO / "server" / "src" / "reader" / "tutor" / "prompts" / "answer-v1.md"
    data = path.read_bytes()
    assert len(data) <= params.SYSTEM_BYTES_MAX
    text, sha = answer.system_prompt()
    assert text == data.decode("utf-8")
    assert sha == hashlib.sha256(data).hexdigest()
    lowered = text.lower()
    assert "data" in lowered and "never an instruction" in lowered
    assert "insufficient" in text and "quote" in text


# ---- VEC-TUTOR-3: cost, derived here


def test_cost_derivation() -> None:
    answer = need("reader.tutor.answer")
    params = need("reader.tutor.params")
    from reader.agent.pricing import load_pricing

    inputs = COST["inputs"]
    rates = load_pricing().models[inputs["model"]]
    for name, price in inputs["price_per_mtok"].items():
        assert rates[name] == D(str(price)), name
    b = bounds()
    tokens = inputs["cli_overhead_tokens"] + inputs["system_bytes_max"] + inputs["prompt_bytes_max"]
    assert inputs["cli_overhead_tokens"] == b["cli_overhead_tokens"]
    assert inputs["system_bytes_max"] == b["system_bytes_max"]
    assert inputs["prompt_bytes_max"] == b["prompt_bytes_max"]
    assert inputs["max_output_tokens"] == b["max_output_tokens"]
    reserve = (D(tokens) * rates["cache_write_1h"] + D(inputs["max_output_tokens"]) * rates["output"]) / D(10**6)
    assert answer.max_reserve_usd(inputs["model"]) == reserve
    assert answer.floor_usd(inputs["model"], b["system_bytes_max"], b["prompt_bytes_max"]) == reserve
    assert answer.floor_usd(inputs["model"], 800, 10000) < reserve
    assert params.QUESTION_CAP_USD >= inputs["attempts_max"] * reserve
    typical = inputs["typical"]
    usd = (
        D(typical["prompt_tokens"] + typical["fixed_tokens"]) * rates["cache_write_1h"]
        + D(typical["output_tokens"]) * rates["output"]
    ) / D(10**6)
    nfr = next(n for n in spec()["nfr"] if n["id"] == "NFR-TUTOR-2")
    assert usd <= D(str(nfr["threshold"]))
    # The 40-question bench (VEC-TUTOR-7): typically one UTC day, at worst-case reserves at most two.
    assert math.floor(params.DAILY_CAP_USD / usd) >= 40
    assert 2 * math.floor(params.DAILY_CAP_USD / reserve) >= 40


# ---- VEC-TUTOR-4: question state


@pytest.mark.parametrize("case", STATUS, ids=lambda c: f"{c['row']}-{c['job']}-{c['age_s']}")
def test_status_map(case: dict[str, Any]) -> None:
    store = need("reader.tutor.store")
    derived = store.derive(case["row"], case["job"], case["age_s"])
    expect = case["expect"]
    assert derived.state == expect["state"]
    # A code is derived only for a queued row that becomes terminal; a stored terminal row keeps its own.
    want = expect.get("error_code")
    if case["row"] == "queued" and expect["state"] == "expired":
        want = "expired"
    assert derived.error_code == want
    if "busy" in expect:
        assert derived.busy is expect["busy"]


# ---- FR-TUTOR-13: the DoD hook

_TEST_NAME = re.compile(r"(?:async def|def) (test_\w+)|test\(\s*'([^']+)'")


def _test_ids() -> set[str]:
    found: set[str] = set()
    roots = [REPO / "server" / "tests" / "tutor", REPO / "web" / "src" / "tutor", REPO / "e2e" / "web" / "tutor"]
    for root in roots:
        for path in Path(root).rglob("*"):
            if path.suffix in {".py", ".ts", ".tsx"} and path.is_file():
                for a, b in _TEST_NAME.findall(path.read_text(encoding="utf-8")):
                    found.add(a or b)
    return found


def test_dod_hook_lists_existing_tests() -> None:
    from ruamel.yaml import YAML

    path = REPO / "server" / "src" / "reader" / "tutor" / "dod.yaml"
    dod = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    assert dod["operation"] == "ask_tutor"
    criteria = {c["name"]: c for c in dod["criteria"]}
    assert set(criteria) == {
        "functional", "grounding", "injection", "isolation", "cost", "latency", "accessibility"
    }
    ids = _test_ids()
    for name, criterion in criteria.items():
        assert criterion["proves"], name
        assert criterion["tests"], name
        for test_id in criterion["tests"]:
            assert test_id in ids, f"{name}: {test_id} does not exist"
