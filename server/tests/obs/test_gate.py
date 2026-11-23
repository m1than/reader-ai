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
    assert routes.decide(origin, method, site, mode, dest, role) == status


def test_missing_forwarded_method_counts_as_get() -> None:
    routes = require("reader.obs.routes")
    assert routes.decide(None, None, None, None, None, "owner") == 204
