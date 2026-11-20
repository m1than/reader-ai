"""Routes as data and the request context every handler receives (SRV-AUTH FR-AUTH-10)."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, Literal

from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from reader.auth.policy import SESSION_COOKIE, Settings
from reader.auth.store import SessionUser

Auth = Literal["public", "session", "recent_session"]
# Structural bound on JSON bodies: the largest stage-1 body is a registration response of a few KiB.
MAX_BODY_BYTES: Final = 65536


@dataclass(frozen=True)
class Ctx:
    request: Request
    settings: Settings
    engine: AsyncEngine
    user: SessionUser | None

    def cookie(self, name: str) -> str | None:
        return self.request.cookies.get(name)

    @property
    def session(self) -> str | None:
        return self.cookie(SESSION_COOKIE)

    @property
    def signed_in(self) -> SessionUser:
        """The user of a `session` or `recent_session` route; the dispatcher guarantees one."""
        if self.user is None:
            raise LookupError("no session user on a session route")
        return self.user

    async def json(self) -> Any:
        """The JSON body, or None when it is unparseable: over MAX_BODY_BYTES (read no further), not JSON,
        not UTF-8, or nested deeper than the parser's recursion limit."""
        declared = self.request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return None
        body = bytearray()
        async for chunk in self.request.stream():
            body += chunk
            if len(body) > MAX_BODY_BYTES:
                return None
        try:
            return json.loads(body)
        except ValueError, RecursionError:
            return None


Handler = Callable[[Ctx], Awaitable[Response]]


@dataclass(frozen=True)
class Route:
    method: Literal["GET", "POST"]
    path: str
    auth: Auth
    handler: Handler


def error(status: int, code: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status, headers=headers)


def set_cookie(response: Response, name: str, value: str, max_age: int) -> None:
    """`__Host-` cookie: Secure, HttpOnly, SameSite=Lax, Path=/, no Domain (INV-AUTH-3)."""
    response.set_cookie(name, value, max_age=max_age, path="/", secure=True, httponly=True, samesite="lax")
