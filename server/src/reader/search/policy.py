"""SRV-SEARCH FR-SEARCH-34: the retrieval policy as data (routes, budgets, gate, judge, thresholds).

`policy/policy.json` is validated by `policy/policy.schema.json` (Draft 2020-12) plus the checks a schema
cannot state: budgets against `budget_min`, filter values against SRV-INGEST's facets file, `l5_boost` on
tutor routes only, a route for every channel and question type, and the regex time bound of every question
rule. Any failure is `policy_invalid`; there is no partial policy. Code reads values from here only.
"""

from __future__ import annotations

import functools
import hashlib
import importlib
import json
import math
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import jsonschema

from reader.agent.ledger import CHARS_PER_TOKEN_FLOOR

HERE: Final = Path(__file__).resolve().parent / "policy"
POLICY_FILE: Final = HERE / "policy.json"
SCHEMA_FILE: Final = HERE / "policy.schema.json"
PROMPTS_DIR: Final = Path(__file__).resolve().parents[1] / "agent" / "prompts"
CHANNELS: Final = ("mcp", "owner", "tutor", "pipeline")
QUESTION_TYPES: Final = ("exact", "thematic", "current_state", "fact_check")
REGEX_BUDGET_S: Final = 0.05  # proposed_bounds regex_time: 50 ms on a 1,000-character adversarial input
_ADVERSARIAL: Final = (
    "a" * 1000, "a" * 999 + "!", "1" * 999 + "!", "BIP-" * 250, "OP_" * 333, "0x" + "f" * 998, " -_:" * 250,
    "б" * 999 + "!", ("текущ" + " ") * 166, "\\" * 1000,  # noqa: RUF001
)
_FILTER_FACETS: Final = {"role": "role", "form": "form", "reliability_min": "reliability"}


class PolicyError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code} {detail}".strip())
        self.code = code


@dataclass(frozen=True)
class Policy:
    data: Mapping[str, Any]
    facets_sha256: str

    def route(self, channel: str, question_type: str) -> Mapping[str, Any]:
        route: Mapping[str, Any] = self.data["routes"][f"{channel}.{question_type}"]
        return route

    def channel(self, channel: str) -> Mapping[str, int]:
        found: Mapping[str, int] = self.data["channels"][channel]
        return found


def canonical(value: Any) -> bytes:
    """The module's canonical JSON: sorted keys, no spaces, non-ASCII kept, UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def estimate(text: str) -> int:
    """SRV-AGENT's estimator: ceil(code points / chars_per_token_floor)."""
    return math.ceil(len(text) / float(CHARS_PER_TOKEN_FLOOR))


def _repeats(tree: Any) -> list[tuple[int, Any]]:
    """Every (max, body) repeat of a parsed pattern, nested groups, branches and assertions walked."""
    found: list[tuple[int, Any]] = []
    for op, arg in tree:
        name = str(op)
        if name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
            found.append((int(arg[1]), arg[2]))
            found.extend(_repeats(arg[2]))
        elif name in ("SUBPATTERN", "ASSERT", "ASSERT_NOT"):
            found.extend(_repeats(arg[-1]))
        elif name == "ATOMIC_GROUP":
            found.extend(_repeats(arg))
        elif name == "BRANCH":
            for branch in arg[1]:
                found.extend(_repeats(branch))
    return found


def _nested_repeat(pattern: str) -> bool:
    """A repeat of more than one inside another (star height > 1) can backtrack super-linearly."""
    tree = importlib.import_module("re._parser").parse(pattern)
    return any(top > 1 and any(inner > 1 for inner, _ in _repeats(body)) for top, body in _repeats(tree))


def _regex_ok(pattern: str) -> bool:
    try:
        compiled = re.compile(pattern)
    except re.error:
        return False
    if _nested_repeat(pattern):
        return False
    for text in _ADVERSARIAL:
        start = time.perf_counter()
        compiled.search(text)
        if time.perf_counter() - start > REGEX_BUDGET_S:
            return False
    return True


def _check(data: Mapping[str, Any], facets: Any) -> None:
    floor = data["budget_min"]
    for name in CHANNELS:
        channel = data["channels"][name]
        if not floor <= channel["budget_default"] <= channel["budget_max"]:
            raise PolicyError("policy_invalid", f"channel {name}")
    routes = data["routes"]
    if set(routes) != {f"{c}.{t}" for c in CHANNELS for t in QUESTION_TYPES}:
        raise PolicyError("policy_invalid", "routes")
    for route_id, route in routes.items():
        if "l5_boost" in route and not route_id.startswith("tutor."):
            raise PolicyError("policy_invalid", f"l5_boost {route_id}")
        for key, facet in _FILTER_FACETS.items():
            value = route["filters"][key]
            if value is not None and value not in (facets.facets[facet].values or ()):
                raise PolicyError("policy_invalid", f"filter {route_id}")
    rules = data["question_rules"]
    patterns = [*rules["identifier"], *(p for group in rules["current_state"].values() for p in group)]
    if not all(_regex_ok(p) for p in patterns):
        raise PolicyError("policy_invalid", "question_rules")


def load(path: Path | None = None, facets_path: Path | None = None) -> Policy:
    from reader.ingest import facets as ingest_facets

    try:
        data = json.loads((path or POLICY_FILE).read_text(encoding="utf-8"))
        schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(data)
        loaded_facets = ingest_facets.load(facets_path)
    except (OSError, ValueError, jsonschema.ValidationError, ingest_facets.FacetsError) as error:
        raise PolicyError("policy_invalid", type(error).__name__) from None
    _check(data, loaded_facets)
    return Policy(data, loaded_facets.sha256)


@functools.cache
def policy() -> Policy:
    """The process's policy, loaded once (api and worker start call it, so a bad file stops them)."""
    return load()


def find_routes() -> tuple[str, ...]:
    """SRV-OBS's `find_routes` enum."""
    return tuple(sorted(policy().data["routes"]))


def _prompts(directory: Path) -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.iterdir()) if p.is_file()
    }


def effective_slots() -> dict[str, str]:
    """The judge slots' model bindings from the effective config (the lane's READER_CONFIG)."""
    from reader.agent import judges
    from reader.agent.lane import _config
    from reader.agent.pricing import resolve_model

    try:
        tasks = _config().tasks
    except Exception as error:  # an unreadable config: the caller answers config_unavailable
        raise PolicyError("config_unavailable", type(error).__name__) from None
    return {s: resolve_model(tasks[s].model) for s in sorted(judges.JUDGE_SLOTS) if s in tasks}


def declared(
    loaded: Policy | None = None,
    *,
    slots: Mapping[str, str] | None = None,
    embedding_models: Sequence[str] | None = None,
    prompts_dir: Path | None = None,
) -> dict[str, Any]:
    """FR-OBS-21's declared inputs over this policy (the closed key set; FR-SEARCH-34's deviation)."""
    from reader.search.params import MODEL_ID

    found = loaded or policy()
    data = found.data
    return {
        "routes": dict(data),
        "thresholds": dict(data["thresholds"]),
        "k": data["k_max"],
        "rrf_k": data["rrf_k"],
        "facets_sha256": found.facets_sha256,
        "slots": dict(slots if slots is not None else effective_slots()),
        "embedding_models": list(embedding_models if embedding_models is not None else [MODEL_ID]),
        "prompts": _prompts(prompts_dir or PROMPTS_DIR),
    }


def policy_version(loaded: Policy | None = None, **kw: Any) -> str:
    from reader.obs.versions import policy_version as version_of

    return version_of(declared(loaded, **kw))
