"""The owner's read of the audit table (SRV-EVENTS FR-EVENTS-31), declared as data for SRV-AUTH's ROUTERS."""

from __future__ import annotations

import re
from typing import Final

from sqlalchemy import text
from starlette.responses import JSONResponse, Response

from reader.api.http import Ctx, Route, error

PAGE: Final = 200
_POSITIVE: Final = re.compile(r"[1-9][0-9]{0,17}")


async def list_audit(ctx: Ctx) -> Response:
    if ctx.signed_in.role != "owner":
        return error(403, "owner_only")
    raw = ctx.request.query_params.get("before")
    if raw is not None and _POSITIVE.fullmatch(raw) is None:
        return error(400, "before_invalid")
    before = int(raw) if raw is not None else None
    async with ctx.engine.connect() as conn:
        rows = list(
            await conn.execute(
                text(
                    "SELECT id, at, actor_id, action, object_kind, object_id, detail FROM audit_log"
                    " WHERE (CAST(:b AS bigint) IS NULL OR id < :b) ORDER BY id DESC LIMIT :n"
                ),
                {"b": before, "n": PAGE + 1},
            )
        )
    page = rows[:PAGE]
    return JSONResponse(
        {
            "rows": [
                {
                    "id": r[0],
                    "at": r[1].isoformat(),
                    "actor_id": str(r[2]),
                    "action": r[3],
                    "object_kind": r[4],
                    "object_id": r[5],
                    "detail": r[6],
                }
                for r in page
            ],
            "next_before": page[-1][0] if len(rows) > PAGE else None,
        }
    )


ROUTES: tuple[Route, ...] = (Route("GET", "/api/audit", "session", list_audit),)
