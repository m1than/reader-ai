"""SRV-OBS [unit]: the owner gate's decision table (FR-OBS-9, INV-OBS-5); the route on real sessions is in
tests/auth/test_obs_wiring.py.

@trace FR-OBS-9 INV-OBS-5
"""

from __future__ import annotations

import pytest

from .conftest import require

DASH = "https://localhost:8443"

CASES = [
    # origin, forwarded method, sec-fetch-site, mode, dest, role -> status
    (None, "GET", None, None, None, "owner", 204),
    (None, "GET", "none", "navigate", "document", "owner", 204),
    (None, "GET", "same-origin", "cors", "empty", "owner", 204),
    (DASH, "POST", "same-origin", "cors", "empty", "owner", 204),
    (DASH, "DELETE", None, None, None, "owner", 204),
    (None, "HEAD", None, None, None, "owner", 204),
    (None, "GET", None, None, None, None, 401),
    (None, "GET", None, None, None, "learner", 403),
    # Origin rules
    ("https://localhost:3000", "GET", None, None, None, "owner", 403),
    ("http://localhost:8443", "GET", None, None, None, "owner", 403),
    ("https://localhost", "POST", "same-site", "cors", "empty", "owner", 403),
    ("https://localhost:3000", "POST", "same-site", "cors", "empty", "owner", 403),
    (None, "POST", None, None, None, "owner", 403),
    (None, "PUT", "same-origin", "cors", "empty", "owner", 403),
    # Sec-Fetch-Site: only a top-level GET navigation may arrive same-site (a click from /observability)
    (None, "GET", "same-site", "navigate", "document", "owner", 204),
    (None, "HEAD", "same-site", "navigate", "document", "owner", 403),
    (None, "GET", "same-site", "no-cors", "script", "owner", 403),
    (None, "GET", "same-site", "no-cors", "image", "owner", 403),
    (None, "GET", "same-site", "no-cors", "empty", "owner", 403),
    (None, "GET", "same-site", "cors", "empty", "owner", 403),
    (None, "GET", "same-site", "navigate", "iframe", "owner", 403),
    (None, "GET", "cross-site", "navigate", "document", "owner", 403),
    (None, "GET", "cross-site", "no-cors", "image", "owner", 403),
    # The checks come before the session: a refused request never reveals whether the cookie is valid.
    ("https://localhost:3000", "GET", None, None, None, None, 403),
    (None, "GET", "cross-site", "navigate", "document", None, 403),
    # Code review round 1 survivors: the navigation exception needs mode `navigate`, dest `document` and GET;
    # OPTIONS is no safe method; the dashboard origin is compared exactly (no case folding, no trailing slash).
    (None, "GET", "same-site", "cors", "document", "owner", 403),
    (None, "GET", "same-site", "navigate", "empty", "owner", 403),
    (None, "POST", "same-site", "navigate", "document", "owner", 403),
    (None, "OPTIONS", None, None, None, "owner", 403),
    (None, "options", "same-origin", "cors", "empty", "owner", 403),
    (None, "get", None, None, None, "owner", 204),
    ("HTTPS://LOCALHOST:8443", "POST", None, None, None, "owner", 403),
    ("https://localhost:8443/", "POST", None, None, None, "owner", 403),
    ("HTTPS://LOCALHOST:8443", "GET", None, None, None, "owner", 403),
    (DASH, "GET", "same-site", "no-cors", "image", "owner", 403),
    (DASH, "GET", "cross-site", "cors", "empty", "owner", 403),
    (DASH, "POST", None, None, None, None, 401),
    (DASH, "POST", None, None, None, "learner", 403),
]


@pytest.mark.parametrize(("origin", "method", "site", "mode", "dest", "role", "status"), CASES)
def test_decision_table(
    origin: str | None,
    method: str,
    site: str | None,
    mode: str | None,
    dest: str | None,
    role: str | None,
    status: int,
) -> None:
    routes = require("reader.obs.routes")
    assert routes.DASHBOARD_ORIGIN == DASH
    assert routes.decide(origin, method, site, mode, dest, role, "mlflow") == status


def test_missing_forwarded_method_counts_as_get() -> None:
    routes = require("reader.obs.routes")
    assert routes.decide(None, None, None, None, None, "owner", "mlflow") == 204


JAEGER = "https://localhost:8444"
APP = "https://localhost"
SITE_CASES = [
    # X-Reader-Site (set by Caddy), origin, forwarded method, role -> status (FR-OBS-9, -19; INV-OBS-5 per site)
    ("mlflow", DASH, "POST", "owner", 204),
    ("mlflow", JAEGER, "POST", "owner", 403),
    ("mlflow", JAEGER, "GET", "owner", 403),
    ("jaeger", JAEGER, "POST", "owner", 204),
    ("jaeger", DASH, "POST", "owner", 403),
    ("jaeger", DASH, "GET", "owner", 403),
    ("jaeger", None, "GET", "owner", 204),
    ("jaeger", None, "POST", "owner", 403),
    ("jaeger", None, "GET", None, 401),
    ("app", None, "GET", "owner", 204),  # https://localhost/api/obs/gate answers as before
    ("app", APP, "GET", "learner", 403),
    ("app", DASH, "GET", "owner", 403),
    (None, None, "GET", "owner", 403),  # no site header: never came through Caddy
    ("", DASH, "GET", "owner", 403),
    ("Mlflow", DASH, "GET", "owner", 403),
    ("other", None, "GET", "owner", 403),
    (None, None, "GET", None, 403),  # refused before the session is looked at
]


@pytest.mark.parametrize(("reader_site", "origin", "method", "role", "status"), SITE_CASES)
def test_per_site_origins(reader_site: str | None, origin: str | None, method: str, role: str | None,
                          status: int) -> None:
    rules = require("reader.obs.gate_rules")
    assert rules.SITE_ORIGINS == {"mlflow": DASH, "jaeger": JAEGER, "app": APP}
    assert rules.decide(origin, method, None, None, None, role, reader_site) == status
