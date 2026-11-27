"""`POST /api/search`, `/api/find` and `/api/open` (SRV-SEARCH FR-SEARCH-8), declared as data for SRV-AUTH's
ROUTERS.

Owner only (a learner gets 404, like a missing route). The query arrives in the JSON body, never in a URL; the
routes log lengths, counts and codes, never the query or a card. find and open run on the owner channel, set
here; the client cannot name a channel, a question type or a judge context.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Final

from starlette.responses import JSONResponse, Response

from reader.agent.lane import _config
from reader.api.http import Ctx, Route, error
from reader.config import ConfigError
from reader.search import params
from reader.search import policy as policy_mod
from reader.search.corpus import StoreCorpus
from reader.search.find import Caller, Deps, FindError, find
from reader.search.judge_wait import Waiter
from reader.search.opening import open_
from reader.search.policy import PolicyError
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


# ---- FR-SEARCH-8 revised: find and open on the owner channel, set here, never taken from the client ----

CHANNEL: Final = "owner"
FIND_KEYS: Final = frozenset({"q", "k", "budget", "filters"})
OPEN_KEYS: Final = frozenset({"id", "depth", "budget"})


def _has_provider(handle: str) -> bool:
    try:
        users = _config().users
    except Exception:  # an unreadable config: no provider handle, the judge degrades no_provider
        return False
    return any(u.handle == handle and u.providers for u in users)


def _caller(ctx: Ctx) -> Caller:
    user = ctx.signed_in
    return Caller(user.id, (user.role,), _has_provider(user.handle))


def _refused(route: str, code: str) -> Response:
    log.info("%s refused: %s", route, code)
    if code in ("policy_invalid", "config_unavailable"):
        return error(503, "config_unavailable")
    return error(404 if code == "not_found" else 400, code)


async def post_find(ctx: Ctx) -> Response:
    if ctx.signed_in.role != "owner":
        return error(404, "not_found")
    body = await ctx.json()
    if not isinstance(body, dict) or not set(body) <= FIND_KEYS or not isinstance(body.get("q"), str):
        return _refused("find", "query_invalid")
    filters = body.get("filters", {})
    if not isinstance(filters, dict):
        return _refused("find", "filter_invalid")
    try:
        policy, version = policy_mod.current()
        jobs = getattr(ctx.request.app.state, "jobs", None)
        judge = None if jobs is None else Waiter(engine=ctx.engine, enqueue=jobs.enqueue, policy=policy)
        async with ctx.engine.connect() as conn:
            deps = Deps(corpus=StoreCorpus(conn, policy), judge=judge, policy=policy,
                        policy_version=lambda: version)
            result = await find(_caller(ctx), CHANNEL, body["q"], deps=deps, filters=filters,
                                k=body.get("k"), budget=body.get("budget"))
    except (FindError, PolicyError) as refused:
        return _refused("find", refused.code)
    log.info("find q_len=%d cards=%d judge=%s degraded=%s", len(body["q"]), len(result["cards"]),
             result.get("judge_source") or result.get("judge_skip"), result["degraded"])
    return JSONResponse(result)


async def post_open(ctx: Ctx) -> Response:
    if ctx.signed_in.role != "owner":
        return error(404, "not_found")
    body = await ctx.json()
    if not isinstance(body, dict) or not set(body) <= OPEN_KEYS or "id" not in body:
        return _refused("open", "id_invalid")
    try:
        policy, version = policy_mod.current()
        async with ctx.engine.connect() as conn:
            result = await open_(conn, _caller(ctx), CHANNEL, body["id"], depth=body.get("depth", 1),
                                 budget=body.get("budget"), policy=policy, policy_version=version)
    except (FindError, PolicyError) as refused:
        return _refused("open", refused.code)
    log.info("open cards=%d", len(result["cards"]))
    return JSONResponse(result)


ROUTES: tuple[Route, ...] = (
    Route("POST", "/api/search", "session", post_search),
    Route("POST", "/api/find", "session", post_find),
    Route("POST", "/api/open", "session", post_open),
)
