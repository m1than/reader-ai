"""SRV-AUTH manage amendment, static checks: the sign-in `next` allow-list adds "/settings" (FR-AUTH-47).

@trace FR-AUTH-47
"""

from __future__ import annotations

import re

from auth_helpers import REPO

STATIC = REPO / "server" / "src" / "reader" / "auth" / "static"


def test_passkey_script_allows_settings_by_raw_comparison() -> None:
    js = (STATIC / "passkey.js").read_text(encoding="utf-8")
    assert 'location.search === "?next=/settings"' in js
    assert 'location.search === "?next=/signups"' in js
    # Each allow-listed query maps to its own constant path; nothing else from the query reaches the target.
    assert re.search(r'"\?next=/settings"\s*\?\s*"/settings"', js), "the /settings constant follows its query"
    assert "URLSearchParams" not in js and "decodeURIComponent" not in js
    for arg in re.findall(r"location\.assign\(([^)]*)\)", js):
        assert arg.strip() in {'"/"', "target"}, arg
