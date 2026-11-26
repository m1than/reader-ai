"""SRV-OBS FR-OBS-41 [unit]: the alignment guard on VEC-OBS-8's cases (`reader.obs.align_guard`), its
Hypothesis property (INV-OBS-7), and the pure rules of `reader.obs.align`: the seeded split and its caps, the
error halves, agreement (VEC-OBS-8 `agree`), counting, round results, refusal orders and the end-code
precedence.

@trace FR-OBS-41 INV-OBS-7 VEC-OBS-8
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from .conftest import FIXTURES, load_yaml, require

VEC = load_yaml(FIXTURES / "align-guard.yaml")
SLOT = "judge.eval.relevance"


def _guard() -> Any:
    return require("reader.obs.align_guard")


def _align() -> Any:
    return require("reader.obs.align")


def _inputs(case: dict[str, Any], base: str | None = None) -> Any:
    g = _guard()
    return g.GuardInputs(
        base=base if base is not None else VEC["base"],
        whole=tuple(case["inputs"]["whole"]),
        verbatim=tuple(case["inputs"]["verbatim"]),
    )


# --- VEC-OBS-8 ---


@pytest.mark.parametrize("case", VEC["cases"], ids=[c["id"] for c in VEC["cases"]])
def test_vec_obs_8_verdicts(case: dict[str, Any]) -> None:
    got = _guard().guard(case["proposal"], _inputs(case))
    assert (got or "accept") == case["expect"]


def test_vec_obs_8_nested_delimiter_needs_two_deletions() -> None:
    (case,) = [c for c in VEC["cases"] if c["id"] == "section_delimiter_nested"]
    once = case["proposal"].replace("READER-INSTRUCTIONS", "", 1)
    assert once == case["after_one_deletion"]
    assert _guard().guard(once, _inputs(case)) == "section_delimiter"
    assert _guard().guard(once.replace("READER-INSTRUCTIONS", "", 1), _inputs(case)) is None


def test_vec_obs_8_covers_every_verdict() -> None:
    assert {c["expect"] for c in VEC["cases"]} == {
        "verbatim_8", "whole_input", "demonstration", "section_delimiter", "no_change", "accept"}


def test_normaliser_matches_fr_obs_22() -> None:
    g = _guard()
    assert g.words("Ёлка ЗЕЛЕ­НАЯ, опуш-\nки; x￾y") == ["елка", "зеленая", "опушки", "xy"]
    assert g.GUARD_VERSION


def _value(family: str, raw: Any) -> Any:
    if raw is None:
        return None
    if family == "eval":
        return float(raw)
    return _align().FindOutput(keep=frozenset(raw["keep"]), sufficiency=raw["sufficiency"])


@pytest.mark.parametrize("row", VEC["agree"], ids=[r["id"] for r in VEC["agree"]])
def test_vec_obs_8_agree_table(row: dict[str, Any]) -> None:
    a = _align()
    eq = a.eq_eval if row["family"] == "eval" else a.eq_find
    recorded, rerun = _value(row["family"], row["recorded"]), _value(row["family"], row["rerun"])
    same = None if rerun is None else eq(rerun, recorded, row["threshold"])
    assert a.outcome(row["ok"], same) == row["expect"]


def test_the_base_with_a_current_version_recorded_output_agrees_iff_ok() -> None:
    a = _align()
    assert a.outcome(True, True) == "agree"  # the recorded output is itself the re-run: eq holds
    assert a.outcome(False, True) == "disagree"


# --- INV-OBS-7 ---

INPUT_WORDS = ("ledger", "merkle", "nonce", "wallet", "miner", "clerk", "vault", "ember", "harbor", "квант",
               "берёза", "дерево", "корень", "подпись", "цепочка", "ключ")
FILLER = ("grade", "cards", "strictly", "prefer", "direct", "evidence", "weigh", "carefully", "оценивай", "точно",
          "строго", "прямые")
BASE = " ".join(("judge relevance overall topic score high low question bear share " * 4).split())


def _variant(word: str, how: int) -> str:
    if how == 1:
        return word.upper()
    if how == 2:
        return word.replace("е", "ё") if "е" in word else word.title()  # noqa: RUF001 (ye to yo)
    if how == 3 and len(word) > 3:
        return word[:2] + "­" + word[2:]
    if how == 4 and len(word) > 3:
        return word[:2] + "-\n" + word[2:]
    return word


words_in = st.lists(st.sampled_from(INPUT_WORDS), min_size=2, max_size=5)
card_in = st.lists(st.sampled_from(INPUT_WORDS), min_size=10, max_size=16)
filler_in = st.lists(st.sampled_from(FILLER), min_size=0, max_size=5)
hows = st.lists(st.integers(0, 4), min_size=20, max_size=20)
seps = st.lists(st.sampled_from([" ", " ", "\n"]), min_size=200, max_size=200)


def _text(tokens: list[str], sep: list[str]) -> str:
    out = tokens[0] if tokens else ""
    for i, token in enumerate(tokens[1:]):
        out += sep[i % len(sep)] + token
    return out


def _window(words: list[str], target: list[str]) -> int | None:
    n = len(target)
    for i in range(len(words) - n + 1):
        if words[i:i + n] == target:
            return i
    return None


def _fixpoint(tokens: list[str], sep: list[str], inputs: Any) -> list[str]:
    """Delete the offending words until no refusal applies (INV-OBS-7); `no_change` is left to the padding."""
    g = _guard()
    for _ in range(400):
        code = g.guard(_text(tokens, sep), inputs)
        if code is None or code == "no_change":
            return tokens
        if code == "section_delimiter":
            i = next(i for i, t in enumerate(tokens) if "READER-INSTRUCTIONS" in t)
            tokens = [*tokens[:i], tokens[i].replace("READER-INSTRUCTIONS", "", 1), *tokens[i + 1:]]
            continue
        if code == "demonstration":
            marked = [i for i, t in enumerate(tokens) if g.words(t)[:1] and g.words(t)[0] in g.MARKERS]
            tokens = [t for i, t in enumerate(tokens) if i not in marked[:1]] if marked else tokens[:-1]
            continue
        flat = [g.words(t)[0] for t in tokens]
        if code == "verbatim_8":
            grams = {tuple(w[i:i + 8]) for w in map(g.words, inputs.verbatim + inputs.whole)
                     for i in range(len(w) - 7)}
            i = next(i for i in range(len(flat) - 7) if tuple(flat[i:i + 8]) in grams)
            tokens = tokens[:i] + tokens[i + 8:]
            continue
        assert code == "whole_input"
        for whole in inputs.whole:
            at = _window(flat, g.words(whole))
            if at is not None and g.words(whole):
                tokens = tokens[:at] + tokens[at + len(g.words(whole)):]
                break
    raise AssertionError("no fixpoint")


def _accepts_after_fixpoint(tokens: list[str], sep: list[str], inputs: Any) -> None:
    g = _guard()
    fixed = _fixpoint(list(tokens), sep, inputs)
    # filler only where the fixpoint left the base itself or nothing (to stay distinct from the base)
    distinct = fixed and g.guard(_text(fixed, sep), inputs) != "no_change"
    padded = fixed if distinct else [*fixed, "weigh", "prefer", "strictly"]
    assert g.guard(_text(padded, sep), inputs) is None


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(query=words_in, card=card_in, pre=filler_in, post=filler_in, how=hows, sep=seps, at=st.integers(0, 8))
def test_verbatim_8_refused_then_accepted(query: list[str], card: list[str], pre: list[str], post: list[str],
                                          how: list[int], sep: list[str], at: int) -> None:
    g = _guard()
    inputs = g.GuardInputs(base=BASE, whole=(" ".join(query),), verbatim=(" ".join(card),))
    at = min(at, len(card) - 8)
    window = [_variant(w, how[i]) for i, w in enumerate(card[at:at + 8])]
    tokens = [*pre, *window, *post]
    assert g.guard(_text(tokens, sep), inputs) == "verbatim_8"
    _accepts_after_fixpoint(tokens, sep, inputs)


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(query=words_in, pre=filler_in, post=filler_in, how=hows, sep=seps, answer=st.booleans())
def test_whole_input_refused_then_accepted(query: list[str], pre: list[str], post: list[str], how: list[int],
                                           sep: list[str], answer: bool) -> None:
    g = _guard()
    whole = ("unrelated words here", " ".join(query)) if answer else (" ".join(query),)
    inputs = g.GuardInputs(base=BASE, whole=whole, verbatim=())
    tokens = [*pre, *(_variant(w, how[i]) for i, w in enumerate(query)), *post]
    assert g.guard(_text(tokens, sep), inputs) == "whole_input"
    _accepts_after_fixpoint(tokens, sep, inputs)


@settings(max_examples=40, deadline=None)
@given(pre=filler_in, post=filler_in, marker=st.sampled_from(["Example:", "ПРИМЕР", "input", "Output -", "Q:", "A."]),
       long=st.booleans(), sep=seps)
def test_demonstration_refused_then_accepted(pre: list[str], post: list[str], marker: str, long: bool,
                                             sep: list[str]) -> None:
    g = _guard()
    inputs = g.GuardInputs(base=BASE, whole=(), verbatim=())
    if long:
        tokens = [*pre, *(FILLER * 30), *post]
        sep = [" "]
    else:
        tokens = ["grade", *pre, "\n" + marker, *post]
        sep = [" "]
    assert g.guard(_text(tokens, sep), inputs) == "demonstration"
    _accepts_after_fixpoint(tokens, sep, inputs)


@settings(max_examples=40, deadline=None)
@given(pre=filler_in, post=filler_in, nested=st.integers(0, 3), sep=seps,
       shape=st.sampled_from(["inside", "line_open", "line_close"]))
def test_section_delimiter_refused_then_accepted(pre: list[str], post: list[str], nested: int, sep: list[str],
                                                 shape: str) -> None:
    g = _guard()
    inputs = g.GuardInputs(base=BASE, whole=(), verbatim=())
    mark = "READER-INSTRUCTIONS"
    for _ in range(nested):
        mark = "READER-" + mark + "INSTRUCTIONS" if nested % 2 else "READER-INSTRUC" + mark + "TIONS"
    token = {"inside": f"pre{mark}post", "line_open": f"<<<{mark}", "line_close": f"{mark}>>>"}[shape]
    tokens = ["grade", *pre, token, *post]
    assert g.guard(_text(tokens, sep), inputs) == "section_delimiter"
    _accepts_after_fixpoint(tokens, sep, inputs)


def test_no_change_refused_then_accepted_with_filler() -> None:
    g = _guard()
    inputs = g.GuardInputs(base=BASE, whole=(), verbatim=())
    assert g.guard(BASE, inputs) == "no_change"
    assert g.guard(BASE + "\n", inputs) == "no_change"
    _accepts_after_fixpoint(BASE.split(" "), [" "], inputs)


def test_neutralised_delimiter_is_accepted() -> None:
    g = _guard()
    inputs = g.GuardInputs(base=BASE, whole=(), verbatim=())
    assert g.guard("grade READER_INSTRUCTIONS_DATA strictly", inputs) is None


# --- FR-OBS-41: the pure rules ---


def _ids(n: int) -> list[str]:
    return [f"00000000-0000-4000-8000-{i:012d}" for i in range(n)]


def test_split_is_seeded_by_slot_and_count_and_capped() -> None:
    a = _align()
    ids = _ids(150)
    train, held = a.split(ids, SLOT, 150)
    assert (len(train), len(held)) == (70, 30)
    assert set(train) | set(held) == set(ids[:100]) and not set(train) & set(held)  # newest 100 only
    assert a.split(ids, SLOT, 150) == (train, held)
    assert a.split(ids, SLOT, 200) != (train, held)
    assert a.split(ids, "judge.eval.sufficiency", 150) != (train, held)
    train, held = a.split(_ids(50), SLOT, 50)
    assert (len(train), len(held)) == (35, 15)


def test_too_few_labels_below_ten_in_either_split() -> None:
    a = _align()
    train, held = a.split(_ids(33), SLOT, 50)
    assert len(held) < 10
    assert a.step0_refusal(frame_ok=True, base_ok=True, slot_supported=True, threshold_present=True,
                           n_train=len(train), n_heldout=len(held), n_errors=3) == "too_few_labels"
    train, held = a.split(_ids(34), SLOT, 50)
    assert len(held) == 10 and len(train) == 24


def test_step0_refusal_order_returns_the_first_that_applies() -> None:
    a = _align()
    every = dict(frame_ok=False, base_ok=False, slot_supported=False, threshold_present=False, n_train=0,
                 n_heldout=0, n_errors=0)
    order = ["frame_changed", "base_changed", "slot_unsupported", "threshold_missing", "too_few_labels",
             "no_training_errors"]
    fixes = [("frame_ok", True), ("base_ok", True), ("slot_supported", True), ("threshold_present", True),
             ("n_train", 20), ("n_errors", 1)]
    state: dict[str, Any] = dict(every)
    for code, (key, value) in zip(order, fixes, strict=True):
        assert a.step0_refusal(**state) == code
        state[key] = value
        if key == "n_train":
            state["n_heldout"] = 10
    assert a.step0_refusal(**state) is None


def test_later_step_refusal_order() -> None:
    a = _align()
    assert a.later_refusal(frame_ok=False, base_ok=False, policy_same=False) == "frame_changed"
    assert a.later_refusal(frame_ok=True, base_ok=False, policy_same=False) == "base_changed"
    assert a.later_refusal(frame_ok=True, base_ok=True, policy_same=False) == "policy_changed"
    assert a.later_refusal(frame_ok=True, base_ok=True, policy_same=True) is None


def test_threshold_keys_per_slot() -> None:
    a = _align()
    assert a.threshold_key("judge.find") == "judge.find.sufficiency_threshold"
    assert a.threshold_key(SLOT) == "judge.eval.relevance.pass_threshold"


def test_error_halves_are_seeded_and_a_half_may_be_empty() -> None:
    a = _align()
    errors = _ids(5)
    one, two = a.halves(errors, SLOT, 50, 1)
    assert sorted(one + two) == sorted(errors) and len(one) == 3 and len(two) == 2
    assert a.halves(errors, SLOT, 50, 1) == (one, two)
    assert a.halves(errors[:1], SLOT, 50, 2)[1] == []  # a half with no error: no call, no candidate


def test_counting_missing_and_rates_over_labels_counted_for_both() -> None:
    a = _align()
    best = {"l1": "agree", "l2": "disagree", "l3": "missing", "l4": "agree"}
    cand = {"l1": "agree", "l2": "agree", "l3": "agree", "l4": "missing"}
    assert a.rates(best, cand) == (0.5, 1.0, 2)  # l1, l2 only
    assert a.missing_share(cand, 4) == 0.25
    assert a.rates({}, cand) == (None, None, 0)


def test_round_result_drops_over_20_percent_missing_and_compares_the_other() -> None:
    a = _align()
    best = {f"l{i}": ("agree" if i < 5 else "disagree") for i in range(10)}
    heavy = {f"l{i}": ("missing" if i < 3 else "agree") for i in range(10)}  # 30 % missing
    good = {f"l{i}": "agree" for i in range(10)}
    got = a.round_result(best, [heavy, good], 10)
    assert (got.promoted, got.scored, got.dropped_missing) == (1, 1, 1)
    edge = {f"l{i}": ("missing" if i < 2 else "agree") for i in range(10)}  # exactly 20 %: compared
    assert a.round_result(best, [edge], 10).dropped_missing == 0


def test_a_tie_keeps_the_current_best_and_the_higher_candidate_wins() -> None:
    a = _align()
    best = {f"l{i}": ("agree" if i < 5 else "disagree") for i in range(10)}
    same = dict(best)
    assert a.round_result(best, [same], 10).promoted is None
    better = {f"l{i}": ("agree" if i < 7 else "disagree") for i in range(10)}
    best_of = {f"l{i}": ("agree" if i < 9 else "disagree") for i in range(10)}
    assert a.round_result(best, [better, best_of], 10).promoted == 1


def test_rounds_are_bounded_and_end_without_improvement() -> None:
    a = _align()
    assert a.next_round(1, improved=True) == 2
    assert a.next_round(2, improved=True) == 3
    assert a.next_round(3, improved=True) is None  # at most 3 rounds
    assert a.next_round(1, improved=False) is None  # no round 2 or 3
    assert a.VEC["limits"]["proposing_steps"] == 2


def test_end_code_precedence() -> None:
    a = _align()
    assert a.end_code(scored_any=True, dropped_missing=1, dropped_guard=1) == "no_change"
    assert a.end_code(scored_any=False, dropped_missing=2, dropped_guard=0) == "provider_error"
    assert a.end_code(scored_any=False, dropped_missing=0, dropped_guard=2) == "all_guard_refused"
    assert a.end_code(scored_any=False, dropped_missing=1, dropped_guard=1) == "provider_error"


def test_final_rates_over_labels_counted_for_both_base_and_final() -> None:
    a = _align()
    base = {"l1": "agree", "l2": "disagree", "l3": "disagree", "l4": "missing"}
    final = {"l1": "agree", "l2": "agree", "l3": "missing", "l4": "agree"}
    assert a.rates(base, final) == (0.5, 1.0, 2)
