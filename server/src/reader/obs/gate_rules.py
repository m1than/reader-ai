"""The owner gate's decision table (SRV-OBS FR-OBS-9, INV-OBS-5), free of any dependency so the [image] edge
test can run it in a bare container. The request-shape checks come before the session, so a refused request
never reveals whether its cookie is valid."""

from __future__ import annotations

from typing import Final

DASHBOARD_ORIGIN: Final = "https://localhost:8443"
_SAFE: Final = ("GET", "HEAD")


def precheck(
    origin: str | None, method: str | None, site: str | None, mode: str | None, dest: str | None
) -> int | None:
    """403 for a request the dashboard must not serve whoever sends it; None when the session decides."""
    forwarded = (method or "GET").upper()
    if origin is not None and origin != DASHBOARD_ORIGIN:
        return 403  # MLflow answers every localhost origin with credentialed CORS
    if forwarded not in _SAFE and origin != DASHBOARD_ORIGIN:
        return 403
    if site is not None and site not in ("same-origin", "none"):
        navigation = site == "same-site" and mode == "navigate" and dest == "document" and forwarded == "GET"
        if not navigation:
            return 403  # only a top-level GET navigation (a click from /observability) may arrive same-site
    return None


def decide(
    origin: str | None,
    method: str | None,
    site: str | None,
    mode: str | None,
    dest: str | None,
    role: str | None,
) -> int:
    refused = precheck(origin, method, site, mode, dest)
    if refused is not None:
        return refused
    if role is None:
        return 401
    return 204 if role == "owner" else 403
