"""SRV-TUTOR [unit]: bounds, the error catalogue and its outcomes, answer validation, grounding, injection, prompt
sizing, the system prompt, cost derivation, the state map and the DoD hook.

@trace FR-TUTOR-3 FR-TUTOR-6 FR-TUTOR-7 FR-TUTOR-8 FR-TUTOR-10 FR-TUTOR-13
@trace INV-TUTOR-2 INV-TUTOR-3
@trace VEC-TUTOR-1 VEC-TUTOR-2 VEC-TUTOR-3 VEC-TUTOR-4 VEC-TUTOR-5
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


def _sent() -> list[Any]:
    answer = need("reader.tutor.answer")
    return [
        answer.Source(alias, value["span"], 60 + i, value["text"])
        for i, (alias, value) in enumerate(ANSWERS["sent"].items())
    ]


# ---- bounds and the catalogue


def test_bounds_match_the_spec() -> None:
    params = need("reader.tutor.params")
    b = bounds()
    assert params.QUESTION_CHARS_MAX == b["question_chars_max"]
    assert params.QUESTION_BYTES_MAX == b["question_bytes_max"]
    assert params.RETRIEVE_K == b["retrieve_k"]
    assert params.SPAN_BYTES_MAX == b["span_bytes_max"]
    assert params.CELL_BYTES_MAX == b["cell_bytes_max"]
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
    checked = answer.check(answer.parse(raw), _sent())
    assert [list(b.kept) for b in checked.blocks] == [b["kept"] for b in expect["blocks"]]
    assert [b.supported for b in checked.blocks] == [b["supported"] for b in expect["blocks"]]
    assert checked.grounded is expect["grounded"]


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
    source = answer.Source("s1", "a" * 64 + ":0", 1, text)
    for n, ok in ((4, False), (5, True), (30, True), (31, False)):
        quote = " ".join(f"w{i}" for i in range(n))
        out = {"blocks": [{"text": "x", "spans": ["s1"], "quote": quote}], "insufficient": False}
        assert answer.check(out, [source]).blocks[0].supported is ok


# ---- INV-TUTOR-2: grounding by code, for any output

_ALIAS = st.sampled_from(["s1", "s2", "s3", "s9", "s0", "S1", "a" * 64 + ":1", "b" * 64 + ":7", "", "s1 "])
_BLOCK = st.fixed_dictionaries(
    {"text": st.text(min_size=1, max_size=50), "spans": st.lists(_ALIAS, max_size=3), "quote": st.text(max_size=80)}
)


@settings(max_examples=300, deadline=None)
@given(blocks=st.lists(_BLOCK, min_size=1, max_size=5), insufficient=st.booleans(), pick=st.integers(0, 40))
def test_grounding_property(blocks: list[dict[str, Any]], insufficient: bool, pick: int) -> None:
    answer = need("reader.tutor.answer")
    sent = _sent()
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
    small = answer.build_prompt("q", None, _spans(["a b"] * 9))
    assert len(small.sources) == params.RETRIEVE_K
    assert json.loads(small.text)["cell"] is None


def test_no_source_is_sources_unavailable() -> None:
    answer = need("reader.tutor.answer")
    errors = need("reader.tutor.errors")
    with pytest.raises(errors.TutorError) as failure:
        answer.build_prompt("q", None, [])
    assert failure.value.code == "sources_unavailable"


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
    assert math.floor(params.DAILY_CAP_USD / reserve) >= 20  # a bench run (≤ 21 questions) fits in one day


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
