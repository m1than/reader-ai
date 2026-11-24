"""SRV-FRESH bounds as constants (spec `bounds`); a test compares each to the spec."""

from __future__ import annotations

from decimal import Decimal
from typing import Final

# Sampling (stage a).
SAMPLE_CHARS_MAX: Final = 60000
FRONT_PAGES: Final = 6
FRONT_CHARS_MAX: Final = 8000
SPAN_CHARS_MAX: Final = 1500
CLAIMS_MAX: Final = 20
SUBJECT_WORDS_MAX: Final = 5
SUBJECT_CHARS_MAX: Final = 60
SUBJECT_TOKEN_CHARS_MAX: Final = 24
# Bibliography (FR-FRESH-1).
BIB_TITLE_CHARS_MAX: Final = 200
BIB_AUTHORS_MAX: Final = 10
BIB_AUTHOR_CHARS_MAX: Final = 100
BIB_EDITION_CHARS_MAX: Final = 40
BIB_PUBLISHER_CHARS_MAX: Final = 120
# Searches (stage b).
SEARCHES_MAX: Final = 6
TOPICS_MAX: Final = 40
TOPIC_CHARS_MAX: Final = 80
QUERY_CHARS_MAX: Final = 120
SHINGLE_WORDS: Final = 6
SEARCH_MAX_TURNS: Final = 2
SEARCH_REQUESTS_MAX: Final = 3
SEARCH_OUTPUT_TOKENS: Final = 1000
T_MAX_TOKENS: Final = 20000
SUMMARY_CHARS_MAX: Final = 1500
RESULTS_PER_SEARCH_MAX: Final = 10
RESULT_HOST_CHARS_MAX: Final = 100
RESULT_TITLE_CHARS_MAX: Final = 120
SCAN_OUTPUT_TOKENS: Final = 4000
# Report (stage c).
REPORT_OUTPUT_TOKENS: Final = 4000
REPORT_PROMPT_TOKENS_MAX: Final = 19100
EXPECTED_CHARS_PER_TOKEN: Final = Decimal("2.5")
EXPECTED_SCAN_OUTPUT_TOKENS: Final = 2600
EXPECTED_SEARCH_USD: Final = Decimal("0.012")
EXPECTED_REPORT_PROMPT_TOKENS: Final = 12000
EXPECTED_REPORT_OUTPUT_TOKENS: Final = 2500
STALE_WITHOUT_JOB_S: Final = 60
RECOMMENDATIONS_MIN: Final = 3
RECOMMENDATIONS_MAX: Final = 5
REASONS_MAX: Final = 8
TEXT_CHARS_MAX: Final = 300
TITLE_CHARS_MAX: Final = 200
URL_CHARS_MAX: Final = 2048
# Money.
RUN_CAP_USD: Final = Decimal("1.00")
DAILY_CAP_USD: Final = Decimal("2.00")
SEARCH_RESERVE_FLOOR_USD: Final = Decimal("0.02")
# Time.
REPORT_TTL_DAYS: Final = 90
SCAN_JOB_TIMEOUT_S: Final = 1200
SEARCH_CALL_BUDGET_S: Final = 300
SEARCH_DEADLINE_S: Final = 240
REPORT_CALL_BUDGET_S: Final = 1200
WEB_JOB_TIMEOUT_S: Final = 3000

# Names (FR-FRESH-3, -7, -8, -12).
TASK_SCAN: Final = "freshness_scan"
TASK_WEB: Final = "freshness_web"
TASK_REPORT: Final = "freshness_report"
TASKS: Final = (TASK_SCAN, TASK_WEB, TASK_REPORT)
KIND_SCAN: Final = "freshness.scan"
KIND_WEB: Final = "freshness.web"
MAX_ATTEMPTS: Final = 2
