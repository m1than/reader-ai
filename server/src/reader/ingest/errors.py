"""Fixed error codes of the server-side ingest commands (SRV-INGEST FR-INGEST-11..13)."""

from __future__ import annotations


class IngestError(Exception):
    """A refusal; `code` is fixed text and never carries material text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def import_error(code: str) -> type[Exception]:
    """A permanent import failure whose class name is its code: SRV-EVENTS stores the class name only."""
    return type(code, (Exception,), {"__module__": __name__})
