"""The link set R (FR-FRESH-7, VEC-FRESH-3, INV-FRESH-3): entries from each search run's structured
`UserMessage.tool_use_result`, never from model text.

The first `results_per_search_max` content items of a search become entries `r1`, `r2`, ... (numbered
across the check). An item is dropped (`url_unsafe`) when its URL is not a string, is longer than
`url_chars_max`, holds a control, whitespace, zero-width or bidi character, `urllib.parse.urlsplit` raises
`ValueError` or gives a scheme other than http/https, userinfo, or no host, the port is not an integer in
0..65535, the host holds a character other than a Unicode letter or digit, `-` or `.` (so no `%`), the host's
`idna` encoding raises `UnicodeError`, the ASCII host is longer than `result_host_chars_max`, or one of
its labels is not LDH (letters, digits, hyphens; 1 to 63 characters; no hyphen at either end). Each entry
keeps the URL string as returned, the ASCII host and the sanitised title. The summary is kept sanitised
and cut, in memory only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urlsplit

from reader.freshness import params
from reader.freshness.text import has_removed, sanitise

SCHEMES: Final = frozenset({"http", "https"})
# One label of the ASCII host: letters, digits and hyphens, 1 to 63 characters, no hyphen at either end (LDH).
LDH_LABEL: Final = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
SEARCH_ERROR_PREFIX: Final = "Web search error:"


@dataclass(frozen=True)
class Entry:
    id: str
    url: str
    host: str
    title: str

    def citation(self) -> dict[str, str]:
        return {"url": self.url, "host": self.host, "title": self.title}


@dataclass(frozen=True)
class Found:
    """One search run's contribution: its R entries, its summary and the dropped count."""

    entries: tuple[Entry, ...]
    summary: str
    dropped: Mapping[str, int] = field(default_factory=dict)


def ascii_host(url: object) -> str | None:
    """The ASCII host of a safe http(s) URL, or None (every parser error is caught here)."""
    if not isinstance(url, str) or not url or len(url) > params.URL_CHARS_MAX:
        return None
    if has_removed(url) or any(ch.isspace() for ch in url):
        return None
    try:
        parts = urlsplit(url)
        if parts.scheme not in SCHEMES or parts.username is not None or parts.password is not None:
            return None
        _ = parts.port  # ValueError unless absent or an integer in 0..65535
        host = parts.hostname
        if not host or not all(ch.isalnum() or ch in "-." for ch in host):  # no `%`, `_`, brackets or symbols
            return None
        encoded = host.encode("idna").decode("ascii")
    except ValueError, UnicodeError:
        return None
    if not encoded or len(encoded) > params.RESULT_HOST_CHARS_MAX:
        return None
    if not all(LDH_LABEL.match(label) for label in encoded.split(".")):
        return None
    return encoded


def _items(tool_result: Mapping[str, Any]) -> tuple[list[Any], list[str]]:
    results = tool_result.get("results")
    items: list[Any] = []
    texts: list[str] = []
    if not isinstance(results, list):
        return items, texts
    for part in results:
        if isinstance(part, Mapping) and isinstance(part.get("content"), list):
            items.extend(part["content"])
        elif isinstance(part, str) and not part.startswith(SEARCH_ERROR_PREFIX):
            texts.append(part)
    return items, texts


def collect(tool_results: Sequence[Any], first_k: int) -> Found:
    """R entries from a run's tool results, numbered from `first_k`."""
    items: list[Any] = []
    texts: list[str] = []
    for result in tool_results:
        if isinstance(result, Mapping):
            found_items, found_texts = _items(result)
            items.extend(found_items)
            texts.extend(found_texts)
    entries: list[Entry] = []
    unsafe = 0
    for item in items[: params.RESULTS_PER_SEARCH_MAX]:
        url = item.get("url") if isinstance(item, Mapping) else None
        host = ascii_host(url)
        if host is None or not isinstance(url, str):
            unsafe += 1
            continue
        title = item.get("title") if isinstance(item, Mapping) else None
        clean_title = sanitise(title, params.RESULT_TITLE_CHARS_MAX) if isinstance(title, str) else ""
        entries.append(Entry(f"r{first_k + len(entries)}", url, host, clean_title or host))
    summary = sanitise(" ".join(texts), params.SUMMARY_CHARS_MAX)
    return Found(tuple(entries), summary, {"url_unsafe": unsafe} if unsafe else {})
