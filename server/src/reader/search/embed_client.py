"""The api's and worker's client of the embed service (SRV-SEARCH FR-SEARCH-5, -7)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence

import httpx

from reader.search import params
from reader.search.errors import EmbedUnavailable, SearchError

HEALTH_POLL_S = 2.0  # structural: one probe every 2 s while the model loads


async def embed(kind: str, texts: Sequence[str], timeout_s: float) -> list[list[float]]:
    """Vectors for `texts`. 503, 500, a non-JSON body or a transport error is EmbedUnavailable; a wrong
    model or dimension is `model_mismatch`; any other status is `embed_rejected`."""
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            url = f"{params.embed_url()}/embed"
            response = await client.post(url, json={"kind": kind, "texts": list(texts)})
    except httpx.TransportError as failure:
        raise EmbedUnavailable("embed_unavailable") from failure
    if response.status_code in (500, 503):
        raise EmbedUnavailable("embed_unavailable")
    if response.status_code != 200:
        raise SearchError("embed_rejected")
    try:
        body = response.json()
    except ValueError as failure:
        raise EmbedUnavailable("embed_unavailable") from failure
    if not isinstance(body, dict) or not isinstance(body.get("vectors"), list):
        raise SearchError("model_mismatch")
    vectors: list[list[float]] = body["vectors"]
    if body.get("model") != params.MODEL_ID or len(vectors) != len(texts):
        raise SearchError("model_mismatch")
    if any(len(v) != params.EMBED_DIMENSIONS for v in vectors):
        raise SearchError("model_mismatch")
    return vectors


async def healthy() -> bool:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            return (await client.get(f"{params.embed_url()}/health")).status_code == 200
    except httpx.HTTPError:
        return False


async def wait_healthy(limit_s: float) -> None:
    """Poll /health until 200; EmbedUnavailable after `limit_s` (FR-SEARCH-5 step 2)."""
    deadline = time.monotonic() + limit_s
    while not await healthy():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise EmbedUnavailable("embed_unavailable")
        await asyncio.sleep(min(HEALTH_POLL_S, remaining))
