"""SRV-OBS FR-OBS-21 as amended (ADR-0039 B7a): `policy_version`, the sha256 of the canonical JSON of the
declared retrieval policy only — the route table, thresholds, k, the RRF constant, the facets file's sha256,
the slot-to-model bindings, the embedding model ids and the prompt files' sha256s by name. The code version is
never an input: a key outside these is refused. SRV-SEARCH passes the policy it loaded (`POLICY`, paired
V.kl-search-find); until then there is none, and the version key keeps its three inputs.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Any, Final

POLICY_KEYS: Final = frozenset({
    "routes", "thresholds", "k", "rrf_k", "facets_sha256", "slots", "embedding_models", "prompts",
})
# SRV-SEARCH's loaded policy (effective values, ADR-0036 overrides included), or None before it lands.
POLICY: Callable[[], Mapping[str, Any] | None] = lambda: None  # noqa: E731


def policy_version(policy: Mapping[str, Any]) -> str:
    unknown = set(policy) - POLICY_KEYS
    if unknown:
        raise ValueError("policy_version: undeclared input " + ", ".join(sorted(unknown)))
    canonical = json.dumps(dict(policy), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def current_policy_version() -> str | None:
    policy = POLICY()
    return None if policy is None else policy_version(policy)
