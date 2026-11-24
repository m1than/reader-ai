"""Bounds of browser upload and host pickup (SRV-INGEST revision 2, ADR-0029). All structural except RSS_MAX.

Read through the module (`params.X`) at call time, so tests can lower a bound without touching the code path.
"""

from __future__ import annotations

from typing import Final

UPLOAD_BYTES_MAX: int = 200 * 1024 * 1024  # 209,715,200 bytes, owner 2026-11-23 (about 10 times the pilot)
OPEN_MAX: int = 5  # open uploads (not yet handed to the import)
BYTES_MAX: int = 1 << 30  # bytes of open uploads plus the incoming body
BODY_DEADLINE_S: float = 300.0  # the whole body (408 body_timeout); in the api, not in Caddy (FR-DEPLOY-14)
CHUNK: Final = 1 << 20  # body and fetch chunks
LEASE_S: Final = 120  # a claim's lease, renewed by every progress
HEARTBEAT_S: Final = 30.0  # 4 missed beats expire the lease
ATTEMPTS_MAX: Final = 3  # lease expiries before failed host_lost
WAIT_MAX_S: Final = 25  # next --wait bound
ONLINE_S: Final = 60  # the converter is offline once its last heartbeat is this old
REPLY_MAX: Final = 4096  # bytes of a success line
STRAYS_MAX: Final = 10_000
RSS_MAX: Final = 8 << 30  # twice the pilot's measured 4,025 MiB child peak (NFR-INGEST-1)
WORK_BYTES_MAX: Final = 2 << 30
WORK_ENTRIES_MAX: Final = 100_000
TICK_S: Final = 2.0  # watchdog interval (chosen)
TICK_BUDGET_S: Final = 1.0
UPLOAD_LOCK_KEY: Final = 7_461_022_903_512_201_029  # pg advisory lock: one upload in flight for the instance
INBOX: Final = "inbox"  # <BLOBS_DIR>/inbox/: the only directory the upload route writes
NOTIFY_CHANNEL: Final = "reader_inbox"
LANGUAGES: Final = ("ru", "en", "other")
FAIL_CODES: Final = (
    "fetch_mismatch",
    "not_pdf",
    "inbox_missing",
    "convert_failed",
    "memory_limit",
    "work_limit",
    "timeout",
    "receive_failed",
    "bad_reply",
)
