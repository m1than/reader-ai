"""`POST /api/search` (SRV-SEARCH FR-SEARCH-8), declared as data for SRV-AUTH's ROUTERS.

Owner only (a learner gets 404, like a missing route). The query arrives in the JSON body, never in a URL; the
route logs lengths and counts, never the query.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Final

from starlette.responses import JSONResponse, Response

from reader.api.http import Ctx, Route, error
from reader.config import ConfigError
from reader.search import params
from reader.search.store import material_active, search

log = logging.getLogger("reader.search")
SHA: Final = re.compile(r"^[0-9a-f]{64}$")


def _parse(body: Any) -> tuple[str, int, str | None] | None:
    if not isinstance(body, dict) or not set(body) <= {"q", "k", "material"} or "q" not in body:
        return None
    q, k, material = body["q"], body.get("k", params.K_DEFAULT), body.get("material")
    if not isinstance(q, str) or not 1 <= len(q.strip()) <= params.Q_MAX:
        return None
    if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= params.K_MAX:
        return None
    if material is not None and (not isinstance(material, str) or not SHA.fullmatch(material)):
        return None
    return q.strip(), k, material


async def post_search(ctx: Ctx) -> Response:
    if ctx.signed_in.role != "owner":
        return error(404, "not_found")
    parsed = _parse(await ctx.json())
    if parsed is None:
        log.info("search refused: invalid input")
        return error(400, "invalid_search")
    q, k, material = parsed
    if material is not None and not await material_active(ctx.engine, ctx.signed_in.id, material):
        log.info("search refused: not_found")  # FR-SEARCH-20: the material has no active pack
        return error(404, "not_found")
    try:
        result = await search(ctx.engine, ctx.signed_in.id, q, k, material)
    except (ConfigError, OSError):  # reader.yaml unreadable or invalid: no material language (FR-SEARCH-6)
        log.warning("search refused: config_unavailable")
        return error(503, "config_unavailable")
    log.info("search q_len=%d k=%d results=%d degraded=%s", len(q), k, len(result.results), result.degraded)
    return JSONResponse({"results": [h.to_json() for h in result.results], "degraded": result.degraded})


ROUTES: tuple[Route, ...] = (Route("POST", "/api/search", "session", post_search),)
