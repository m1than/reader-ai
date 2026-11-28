"""SRV-TUTOR bounds as data (spec `bounds`; a test compares every value to the spec)."""

from __future__ import annotations

from decimal import Decimal
from typing import Final

TASK: Final = "tutor"  # reader.yaml task, SRV-AGENT stage and task name
KIND: Final = "tutor.answer"  # SRV-EVENTS job kind (FR-TUTOR-3)
PRIORITY: Final = 100  # above compile stages' default 0 (SRV-EVENTS FR-EVENTS-1 amendment)

QUESTION_CHARS_MAX: Final = 1000
QUESTION_BYTES_MAX: Final = 3000
SPAN_BYTES_MAX: Final = 2000
CELL_BYTES_MAX: Final = 4000
QUESTION_CELL_BYTES_MAX: Final = 8000  # question 3000 + cell 4000 + JSON overhead 1000
SOURCES_MAX: Final = 88  # one alias per span; find gives <= 31 cards + their next = 62 < 88, bytes bind first
PROMPT_BYTES_MAX: Final = 29000  # the largest prompt whose reserve fits 2 attempts in $0.30 (REF-TUTOR-9)
SYSTEM_BYTES_MAX: Final = 2000
QUOTE_WORDS: Final = (5, 30)
SPANS_PER_BLOCK_MAX: Final = 3
BLOCKS_MAX: Final = 20
BLOCK_CHARS_MAX: Final = 4000
MAX_OUTPUT_TOKENS: Final = 2000
CLI_OVERHEAD_TOKENS: Final = 1000
ATTEMPTS_MAX: Final = 2
QUESTION_CAP_USD: Final = Decimal("0.30")
DAILY_CAP_USD: Final = Decimal("3.00")
QUESTION_TTL_S: Final = 600
STALE_WITHOUT_JOB_S: Final = 60
CALL_DEADLINE_S: Final = 20.0
JOB_TIMEOUT_S: Final = 120.0
HISTORY_MAX: Final = 50
