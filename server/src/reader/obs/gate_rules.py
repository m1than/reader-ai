"""The owner gate's decision table (SRV-OBS FR-OBS-9, INV-OBS-5), free of any dependency so the [image] edge
test can run it in a bare container. The request-shape checks come before the session, so a refused request
never reveals whether its cookie is valid. The site that asks (`X-Reader-Site`, set by Caddy on every
forward_auth and on the main site's gate route, overriding the client's) decides the one Origin allowed."""

from __future__ import annotations

from typing import Final

DASHBOARD_ORIGIN: Final = "https://localhost:8443"
SITE_ORIGINS: Final = {
    "mlflow": DASHBOARD_ORIGIN,
    "jaeger": "https://localhost:8444",  # FR-OBS-19 (ADR-0033)
    "app": "https://localhost",  # the main site's /api/obs/gate
}
_SAFE: Final = ("GET", "HEAD")


def precheck(
    origin: str | None,
    method: str | None,
    site: str | None,
    mode: str | None,
    dest: str | None,
    reader_site: str | None = None,
) -> int | None:
    """403 for a request the dashboard must not serve whoever sends it; None when the session decides."""
    expected = SITE_ORIGINS.get(reader_site or "")
    if expected is None:
        return 403  # no or an unknown site: a request that did not come through Caddy
    forwarded = (method or "GET").upper()
    if origin is not None and origin != expected:
        return 403  # MLflow answers every localhost origin with credentialed CORS
    if forwarded not in _SAFE and origin != expected:
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
    reader_site: str | None = None,
) -> int:
    refused = precheck(origin, method, site, mode, dest, reader_site)
    if refused is not None:
        return refused
    if role is None:
        return 401
    return 204 if role == "owner" else 403
