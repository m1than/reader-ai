"""Routes as data and the request context every handler receives (SRV-AUTH FR-AUTH-10)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from reader.auth.policy import SESSION_COOKIE, Settings
from reader.auth.store import SessionUser

Auth = Literal["public", "session", "recent_session"]


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

    async def json(self) -> Any:
        try:
            return await self.request.json()
        except ValueError:
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
