"""Search errors (SRV-SEARCH FR-SEARCH-5): transient classes the job retries, and coded permanent failures."""

from __future__ import annotations


class EmbedUnavailable(Exception):
    """The embed service is unreachable, unhealthy or answered 503 (transient)."""


class IngestNotReady(Exception):
    """The material has no committed ingest yet (transient)."""


class SearchError(Exception):
    """A permanent failure; `code` is fixed text, never a value from the input."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
