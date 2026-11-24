"""FR-AUTH-22: the exact allow-list of routes whose body is not JSON (one entry: the PDF upload).

A request matches an entry only when its method equals the entry's and its raw path string (query excluded,
as received, before any decoding or normalisation) equals the entry's path exactly: never a prefix, a pattern
or a route template. The Origin check of FR-AUTH-12 still applies; the media type must be the entry's.
"""

from __future__ import annotations

from typing import Final

RAW_BODY_ROUTES: Final[tuple[tuple[str, str, str], ...]] = (
    ("POST", "/api/materials/uploads", "application/pdf"),
)


def raw_body_type(method: str, raw_path: bytes) -> str | None:
    """The media type a matching request must carry, or None when the request matches no entry."""
    for entry_method, path, media_type in RAW_BODY_ROUTES:
        if method == entry_method and raw_path == path.encode("ascii"):
            return media_type
    return None
