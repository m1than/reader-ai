"""SRV-SEARCH [unit]: the retrieval policy as data, its schema, declared() and the route table.

@trace FR-SEARCH-34
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any

import pytest

from .conftest import need

CHANNELS = ("mcp", "owner", "tutor", "pipeline")
TYPES = ("exact", "thematic", "current_state", "fact_check")
SLOTS = {"judge.find": "claude-haiku-4-5"}
MODELS = ["BAAI/bge-m3@r1"]


def _policy() -> Any:
    return need("reader.search.policy")


def _shipped() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_policy().POLICY_FILE.read_text(encoding="utf-8"))
    return data


def _load(tmp_path: Path, data: dict[str, Any]) -> Any:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return _policy().load(path)


def _refused(tmp_path: Path, data: dict[str, Any]) -> None:
    with pytest.raises(_policy().PolicyError) as caught:
        _load(tmp_path, data)
    assert caught.value.code == "policy_invalid"


def _expected_route(channel: str, kind: str) -> dict[str, Any]:
    """proposed_bounds routes_initial: L1 everywhere, every filter null, newest_as_of false, rrf false;
    the tutor routes carry l5_boost true (inert)."""
    route: dict[str, Any] = {
        "layers": ["L1"],
        "filters": {"role": None, "form": None, "reliability_min": None, "newest_as_of": False},
        "rrf": False,
    }
    if channel == "tutor":
        route["l5_boost"] = True
    return route


def test_shipped_policy_validates_and_carries_the_initial_values() -> None:
    policy = _policy()
    loaded = policy.load()
    data = loaded.data
    assert data == _shipped()
    assert data["routes"] == {f"{c}.{t}": _expected_route(c, t) for c in CHANNELS for t in TYPES}
    assert data["channels"] == {
        "mcp": {"budget_default": 2000, "budget_max": 2000},
        "owner": {"budget_default": 2000, "budget_max": 10500},
        "tutor": {"budget_default": 5250, "budget_max": 10500},
        "pipeline": {"budget_default": 150000, "budget_max": 150000},
    }
    assert data["card_budgets"] == {"L0": 200, "L1": 600, "L2": {"leaf": 100, "chapter": 300, "root": 500}}
    assert data["gate"] == {"mode": "never_judge", "margin": None}
    assert data["judge"] == {"timeout_s": 2.2, "input_cap_tokens": 3000}
    assert (data["candidates_max"], data["expand_cap"], data["expand_per_card"]) == (50, 50, 2)
    assert (data["expand_decay"], data["preselect_factor"], data["same_point_tau"]) == (0.5, 1.5, None)
    assert data["objective"] == {"identifier_weight": 2, "lexeme_weight": 1, "relevance_weight": 1.0}
    assert (data["rrf_k"], data["w_dense"], data["k_max"], data["budget_min"]) == (60, 5, 50, 400)
    assert (data["open_depth_max"], data["quote_max_chars"], data["quotes_per_card_max"]) == (4, 300, 1)
    assert (data["walk_quote_fraction_max"], data["owner_judge_run_cap_usd"]) == (0.1, 0.01)
    names = ("relevance", "sufficiency", "groundedness", "self_contained", "citation_supports_claim")
    assert data["thresholds"] == {
        "judge.find.sufficiency_threshold": 0.5,
        **{f"judge.eval.{n}.pass_threshold": 0.5 for n in names},
    }


def test_judge_input_cap_equals_the_lane_bound() -> None:
    lane = need("reader.agent.lane")
    assert _shipped()["judge"]["input_cap_tokens"] == lane.JUDGE_FIND_INPUT_TOKENS_MAX


def _closed_objects(data: dict[str, Any]) -> list[dict[str, Any]]:
    route = data["routes"]["mcp.exact"]
    return [
        data, data["channels"], data["channels"]["mcp"], data["routes"], route, route["filters"],
        data["question_rules"], data["question_rules"]["current_state"], data["card_budgets"],
        data["card_budgets"]["L2"], data["objective"], data["gate"], data["judge"], data["thresholds"],
    ]


def test_every_closed_object_refuses_an_extra_key(tmp_path: Path) -> None:
    for index in range(len(_closed_objects(_shipped()))):
        data = _shipped()
        _closed_objects(data)[index]["zz_extra"] = 1
        _refused(tmp_path, data)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("judge", "timeout_s"), 8.1),
        (("channels", "mcp", "budget_default"), 2001),  # default above max
        (("budget_min",), 2001),  # mcp's budgets below budget_min
        (("owner_judge_run_cap_usd",), 0.001),
        (("preselect_factor",), 0.9),
        (("thresholds", "judge.find.sufficiency_threshold"), 1.1),
        (("gate", "mode"), "margin"),  # margin mode without a margin
        (("routes", "mcp.exact", "filters", "role"), "bogus"),  # not a value of the facets file
        (("routes", "mcp.exact", "filters", "form"), "poetry"),
        (("routes", "mcp.exact", "layers"), ["L9"]),
        (("routes", "mcp.exact", "l5_boost"), True),  # tutor only
        (("question_rules", "identifier"), ["(a+)+$"]),  # super-linear backtracking
        (("question_rules", "identifier"), ["("]),  # not a regex
    ],
)
def test_out_of_range_values_are_refused(tmp_path: Path, path: tuple[str, ...], value: Any) -> None:
    data = _shipped()
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    _refused(tmp_path, data)


def test_a_missing_route_is_refused(tmp_path: Path) -> None:
    data = _shipped()
    del data["routes"]["owner.thematic"]
    _refused(tmp_path, data)


def test_valid_variants_load(tmp_path: Path) -> None:
    data = _shipped()
    data["gate"] = {"mode": "margin", "margin": 0.05}
    data["routes"]["mcp.exact"]["filters"]["role"] = "map"
    data["routes"]["mcp.exact"]["filters"]["form"] = "code"
    data["routes"]["mcp.exact"]["filters"]["reliability_min"] = "B"
    assert _load(tmp_path, data).data["gate"]["margin"] == 0.05


def test_find_routes_are_the_route_ids() -> None:
    policy = _policy()
    assert policy.find_routes() == tuple(sorted(f"{c}.{t}" for c in CHANNELS for t in TYPES))


def test_declared_has_fr_obs_21_keys_and_moves_with_each_input(tmp_path: Path) -> None:
    policy = _policy()
    versions = need("reader.obs.versions")
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "judge_find.md").write_bytes(b"judge prompt")
    facets = tmp_path / "facets.yaml"
    facets.write_bytes(need("reader.ingest.facets").FACETS_FILE.read_bytes())

    def declared(data: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        loaded = _load(tmp_path, data or _shipped()) if data is not None else policy.load(facets_path=facets)
        args: dict[str, Any] = {"slots": SLOTS, "embedding_models": MODELS, "prompts_dir": prompts}
        args.update(kw)
        result: dict[str, Any] = policy.declared(loaded, **args)
        return result

    base = declared()
    assert set(base) == set(versions.POLICY_KEYS)
    assert base["routes"] == _shipped()  # the whole parsed file
    assert base["k"] == 50 and base["rrf_k"] == 60
    assert base["prompts"] == {"judge_find.md": hashlib.sha256(b"judge prompt").hexdigest()}
    assert base["slots"] == SLOTS and base["embedding_models"] == ["BAAI/bge-m3@r1"]
    assert base["facets_sha256"] == hashlib.sha256(facets.read_bytes()).hexdigest()
    version = versions.policy_version(base)
    loaded = policy.load(facets_path=facets)
    assert policy.policy_version(loaded, slots=SLOTS, embedding_models=MODELS, prompts_dir=prompts) == version
    changed = _shipped()
    changed["routes"]["mcp.exact"]["rrf"] = True
    assert versions.policy_version(declared(changed)) != version  # a route field
    changed = _shipped()
    changed["expand_decay"] = 0.4
    assert versions.policy_version(declared(changed)) != version  # any declared value
    assert versions.policy_version(declared(slots={"judge.find": "claude-sonnet-5-5"})) != version
    assert versions.policy_version(declared(embedding_models=["BAAI/bge-m3@r2"])) != version
    (prompts / "judge_find.md").write_bytes(b"judge prompt, edited")
    assert versions.policy_version(declared()) != version
    (prompts / "judge_find.md").write_bytes(b"judge prompt")
    facets.write_bytes(facets.read_bytes() + b"\n# a comment\n")
    assert versions.policy_version(declared()) != version  # the facets bytes
    facets.write_bytes(need("reader.ingest.facets").FACETS_FILE.read_bytes())
    assert versions.policy_version(declared()) == version
    assert "code_version" not in base


def test_policy_loads_once_per_process() -> None:
    policy = _policy()
    assert policy.policy() is policy.policy()


def test_question_rules_answer_adversarial_input_within_50_ms() -> None:
    import re

    rules = _shipped()["question_rules"]
    patterns = [*rules["identifier"], *(p for ps in rules["current_state"].values() for p in ps)]
    assert patterns
    inputs = [
        "BIP-" * 250, "OP_" * 333, "0x" + "f" * 998, "a" * 1000, "б" * 1000, "1" * 1000, " -_:" * 250,
        "rfc" * 333, ("текущ" + " ") * 166, "x" * 999 + "!",
    ]
    for pattern in patterns:
        compiled = re.compile(pattern)
        for text in inputs:
            assert len(text) <= 1000
            start = time.perf_counter()
            compiled.search(text)
            assert time.perf_counter() - start <= 0.05, pattern


def test_canonical_json_and_the_estimator() -> None:
    policy = _policy()
    assert policy.canonical({"b": "ключ", "a": [1, 2]}) == '{"a":[1,2],"b":"ключ"}'.encode()
    for text in ("", "a", "ab", "abc", "ключ" * 5):
        assert policy.estimate(text) == math.ceil(len(text) / 2.0)


def test_shipped_policy_copy_is_independent() -> None:
    data = _shipped()
    other = copy.deepcopy(data)
    other["k_max"] = 1
    assert _policy().load().data["k_max"] == 50
