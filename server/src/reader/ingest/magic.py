"""The only inspection of an uploaded file in the stack (SRV-INGEST FR-INGEST-18 step 7, VEC-INGEST-7).

The first 8 bytes are `%PDF-1.[0-7]` or `%PDF-2.0` at offset 0, and the last 1,024 bytes (the whole body when
it is shorter) contain `%%EOF` (ISO 32000-2 7.5.2 and 7.5.5). Nothing else is read: no service parses the PDF.
"""

from __future__ import annotations

import re
from typing import Final

HEAD: Final = re.compile(rb"%PDF-(1\.[0-7]|2\.0)")
TAIL_BYTES: Final = 1024


def check(head: bytes, tail: bytes) -> bool:
    """`head`: the body's first 8 bytes; `tail`: its last 1,024 bytes (or the whole body)."""
    return len(head) == 8 and HEAD.fullmatch(head) is not None and b"%%EOF" in tail[-TAIL_BYTES:]


def is_pdf(body: bytes) -> bool:
    return check(body[:8], body[-TAIL_BYTES:])
