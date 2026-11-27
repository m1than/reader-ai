"""Search parameters and pure helpers (SRV-SEARCH bounds, FR-SEARCH-5, -6, -9; VEC-SEARCH-4).

Every number is sourced or a structural bound with its reason in the spec's bounds block.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Final

from reader.search.embed_service import (
    DIMENSIONS,
    MAX_QUERY_CHARS,
    MAX_SEQ_LENGTH,
    MAX_TEXTS,
    model_id,
)

MODEL_ID: Final = model_id()
CHUNK_SIZE: Final = 1200  # spikes/embeddings-local/bench.py chunk_pages
CHUNK_OVERLAP: Final = 200
CHUNK_MIN: Final = 80
PASSAGE_BATCH: Final = MAX_TEXTS  # 8: the worst batch the 3g limit is sized for
CANDIDATES: Final = 50  # structural: 5 times the default k and 2.2 % of the pilot's chunks
# FR-SEARCH-7: weighted RRF; chosen on v1 (the dev set):
# spec/fixtures/SRV-SEARCH/results/fusion-weight-v1.json
W_DENSE: Final = 5.0
# FR-SEARCH-7: False because the one v2 test left hybrid below dense-only (24 vs 27 of 40 with w 5;
# results/fusion-test-v2.json): the slice serves the dense order, full text only when degraded.
HYBRID: Final = False
K_DEFAULT: Final = 10
K_MAX: Final = 50
Q_MAX: Final = MAX_QUERY_CHARS
QUERY_TIMEOUT_S: Final = 30.0  # one worst batch of 8 (13.29 s at 8 CPUs) + the query's 0.93 s, about 2 times
PASSAGE_TIMEOUT_S: Final = 60.0  # 4.5 times the worst batch of 8
DEFAULT_EMBED_URL: Final = "http://embed:8100"
FTS_CONFIGS: Final = ("russian", "english", "simple")
_LANGUAGES: Final = {"ru": "russian", "en": "english"}
EMBED_DIMENSIONS: Final = DIMENSIONS


def embed_url() -> str:
    """The embed service's base URL; READER_EMBED_URL overrides it (tests and the [local] bench)."""
    return os.environ.get("READER_EMBED_URL", DEFAULT_EMBED_URL).rstrip("/")


def fts_config_for(language: str | None) -> str:
    """FR-SEARCH-6: `ru` → russian, `en` → english, anything else (or no entry) → simple."""
    return _LANGUAGES.get(language or "", "simple")


def params_sha256(fts_config: str) -> str:
    """FR-SEARCH-5: the vector overlay's parameter digest over {max_seq_length, window size, overlap, minimum,
    fts_config}; the model enters the overlay inputs beside it."""
    params = {
        "max_seq_length": MAX_SEQ_LENGTH,
        "window_size": CHUNK_SIZE,
        "overlap": CHUNK_OVERLAP,
        "minimum": CHUNK_MIN,
        "fts_config": fts_config,
    }
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def status_from(job: str, overlay: str) -> str:
    """VEC-SEARCH-4: the job status of the current key by the state of its vector overlay (none, building,
    sealed, active, collected)."""
    if overlay == "collected":
        return "collected"
    if overlay == "active":
        return "ready"
    if overlay == "sealed":
        return {"none": "queued", "succeeded": "queued"}.get(job, job)
    return "dead" if job == "succeeded" else job
