"""The allow-list of SRV-OBS FR-OBS-2 as data (VEC-OBS-1, spec/fixtures/SRV-OBS/attributes.yaml), and its
checks.

Everything the api and worker may export is listed here: span names by pattern, attribute keys with a type, a
value rule and the span classes that may carry them, and the job arguments copied to a job's root span. A test
compares this module with VEC-OBS-1.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

SCOPE: Final = "reader"
FALLBACK_NAME: Final = "reader.unlisted"
MAX_STRING: Final = 128
METHODS: Final = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT", "_OTHER")
UUID: Final = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
TOKENS: Final = (0, 1_000_000_000)

ATTRIBUTES: Final[Mapping[str, Mapping[str, Any]]] = {
    "http.request.method": {"type": "string", "enum": METHODS},
    "http.route": {"type": "string", "enum_from": "route_templates"},
    "http.response.status_code": {"type": "int", "range": (100, 599)},
    "reader.ws.close_code": {"type": "int", "range": (1000, 4999)},
    "reader.job.kind": {"type": "string", "enum_from": "job_kinds"},
    "reader.job.id": {"type": "int", "range": (1, 9223372036854775807)},
    "reader.job.attempt": {"type": "int", "range": (0, 1000)},
    "reader.job.outcome": {"type": "string", "enum": ("succeeded", "skipped", "failed", "dead", "aborted")},
    "reader.error_code": {"type": "string", "enum_from": "error_codes"},
    "reader.error_class": {"type": "string", "pattern": "^[A-Z][A-Za-z0-9]{1,63}$"},
    "reader.run_id": {"type": "string", "pattern": UUID},
    "reader.step": {"type": "int", "range": (0, 999)},
    "reader.stage": {"type": "string", "enum_from": "pipeline_stages"},
    "reader.question_id": {"type": "string", "pattern": UUID},
    "reader.ingest_id": {"type": "string", "pattern": UUID},
    "reader.material_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    "reader.task": {"type": "string", "enum_from": "tasks"},
    "gen_ai.request.model": {"type": "string", "enum_from": "models"},
    "gen_ai.usage.input_tokens": {"type": "int", "range": TOKENS},
    "gen_ai.usage.output_tokens": {"type": "int", "range": TOKENS},
    "gen_ai.usage.cache_read.input_tokens": {"type": "int", "range": TOKENS},
    "gen_ai.usage.cache_write.input_tokens": {"type": "int", "range": TOKENS},
    "gen_ai.operation.name": {"type": "string", "enum": ("invoke_agent",)},
    "gen_ai.provider.name": {"type": "string", "enum": ("anthropic",)},
    "reader.cost_usd": {"type": "float", "range": (0, 1000)},
    "reader.cost_input_usd": {"type": "float", "range": (0, 1000)},
    "reader.stop_reason": {
        "type": "string",
        "enum": ("end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn", "refusal"),
    },
    "reader.lesson.id": {"type": "string", "pattern": UUID},
    "reader.course.id": {"type": "string", "pattern": UUID},
    "reader.tutor.state": {"type": "string", "enum": ("answered", "failed", "refused", "expired")},
    "reader.prompt.version": {"type": "int", "range": (1, 100000)},
    "reader.retrieval.hits": {"type": "int", "range": (0, 1000)},
    "reader.retrieval.candidates": {"type": "int", "range": (0, 10000)},
    "reader.citations.total": {"type": "int", "range": (0, 1000)},
    "reader.citations.valid": {"type": "int", "range": (0, 1000)},
    "reader.claims.total": {"type": "int", "range": (0, 10000)},
    "reader.claims.supported": {"type": "int", "range": (0, 10000)},
    "reader.kernel.cells": {"type": "int", "range": (0, 10000)},
    "reader.kernel.cells_ok": {"type": "int", "range": (0, 10000)},
    "reader.kernel.ms": {"type": "int", "range": (0, 86400000)},
    "reader.kernel.exit": {"type": "int", "range": (-255, 255)},
    "reader.pages": {"type": "int", "range": (0, 100000)},
    "reader.bytes": {"type": "int", "range": (0, 10000000000)},
    "reader.chunks": {"type": "int", "range": (0, 10000000)},
    "reader.embed.vectors": {"type": "int", "range": (0, 10000000)},
    "reader.embed.batch": {"type": "int", "range": (0, 100000)},
}

# FR-OBS-12 (e) and FR-OBS-13: span names listed literally (the session marker and the child spans).
CHILD_SPANS: Final = (
    "job.session", "tutor.retrieve", "tutor.citations", "verify.claims", "verify.lab", "kernel.run",
    "ingest.parse", "ingest.chunk", "embed.batch",
)

KEYS_BY_SPAN: Final[Mapping[str, tuple[str, ...]]] = {
    "http": ("http.request.method", "http.route", "http.response.status_code"),
    "ws": ("http.request.method", "http.route", "reader.ws.close_code"),
    "job": (
        "reader.job.kind", "reader.job.id", "reader.job.attempt", "reader.job.outcome", "reader.error_code",
        "reader.error_class", "reader.run_id", "reader.step", "reader.stage", "reader.question_id",
        "reader.ingest_id", "reader.material_sha256", "reader.lesson.id", "reader.course.id",
        "reader.tutor.state", "reader.prompt.version",
    ),
    "agent.call": (
        "reader.task", "reader.stage", "gen_ai.request.model", "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens", "gen_ai.usage.cache_read.input_tokens",
        "gen_ai.usage.cache_write.input_tokens", "reader.cost_usd", "reader.stop_reason",
        "reader.error_code", "gen_ai.operation.name", "gen_ai.provider.name", "reader.cost_input_usd",
        "reader.lesson.id", "reader.course.id", "reader.step",
    ),
    "job.session": ("reader.run_id", "reader.lesson.id"),
    "tutor.retrieve": ("reader.retrieval.hits", "reader.retrieval.candidates"),
    "tutor.citations": ("reader.citations.total", "reader.citations.valid"),
    "verify.claims": ("reader.claims.total", "reader.claims.supported"),
    "verify.lab": ("reader.kernel.cells", "reader.kernel.cells_ok", "reader.kernel.ms"),
    "kernel.run": ("reader.kernel.cells", "reader.kernel.exit"),
    "ingest.parse": ("reader.pages", "reader.bytes"),
    "ingest.chunk": ("reader.chunks",),
    "embed.batch": ("reader.embed.vectors", "reader.embed.batch"),
}

# FR-OBS-5 (b): argument fields copied from a job's validated args to its root span; no other field is read.
JOB_ARGS: Final[Mapping[str, Mapping[str, str]]] = {
    "pipeline.stage": {"run_id": "reader.run_id", "step": "reader.step", "stage": "reader.stage"},
    "pipeline.verify": {"run_id": "reader.run_id", "step": "reader.step", "stage": "reader.stage"},
    "tutor.answer": {"question_id": "reader.question_id"},
    "ingest.import": {"material_sha256": "reader.material_sha256"},
    "search.embed": {"material_sha256": "reader.material_sha256", "ingest_id": "reader.ingest_id"},
    "obs.retention": {},
    "obs.feedback": {},  # FR-OBS-14: its args name another job and a catalogue entry; none reaches the span
    "obs.prompts": {},  # FR-OBS-15
    "freshness.scan": {},  # SRV-FRESH paired change: no argument (the report id) is copied
    "freshness.web": {},
}

# The declared error catalogues: the `errors` lists of SRV-AGENT, SRV-TUTOR and SRV-PIPELINE (a test compares
# them with the specs) and SRV-EVENTS' job codes.
JOB_CODES: Final = frozenset({
    "exhausted", "stalled", "orphaned", "timeout", "session_lost", "wrapper_error", "unknown_kind",
    "no_key_row",
})
SPEC_CODES: Final = frozenset({
    "already_started", "body_not_json", "body_not_utf8", "budget_exhausted", "cap_invalid", "cell_unknown",
    "citation_unknown_span", "compile_running", "course_deleted", "daily_budget_exhausted",
    "deadline_exceeded", "expired", "input_not_ready", "labs_unverifiable", "lesson_inactive",
    "lesson_unknown", "lost",
    "model_mismatch", "monthly_budget_exhausted", "no_material", "no_source_spans", "not_found", "not_owner",
    "not_running", "origin_conflict",
    "output_cap", "output_invalid", "output_truncated", "provider_missing", "provider_unavailable",
    "question_invalid", "rate_limited", "request_cap", "run_token_unknown", "sdk_error", "search_cap",
    "source_too_large", "sources_unavailable", "started_lesson_dropped", "task_budget_exhausted",
    "tools_mismatch", "tools_not_allowed", "tutor_busy", "tutor_failed", "unknown_model", "unknown_revision",
    "upstream_retry", "usage_unmapped", "verify_unavailable",
})
ERROR_CODES: Final = SPEC_CODES | JOB_CODES
_KEY_NAME: Final = re.compile(r"[a-z][a-z0-9_.]{0,63}")


@dataclass(frozen=True)
class Catalogs:
    """The code-declared sets the `enum_from` rules read; computed at setup, never from request data."""

    route_templates: frozenset[str]
    job_kinds: frozenset[str]
    pipeline_stages: frozenset[str]
    tasks: frozenset[str]
    models: frozenset[str]


def key_name(key: object) -> str:
    """A key as it may be logged: itself when it has the shape of a key, else `<invalid>`."""
    return key if isinstance(key, str) and _KEY_NAME.fullmatch(key) else "<invalid>"


class AllowList:
    def __init__(self, catalogs: Catalogs) -> None:
        self._enums: dict[str, frozenset[str]] = {
            "route_templates": frozenset(catalogs.route_templates) | {"other"},
            "job_kinds": frozenset(catalogs.job_kinds),
            "pipeline_stages": frozenset(catalogs.pipeline_stages),
            "tasks": frozenset(catalogs.tasks),
            "models": frozenset(catalogs.models),
            "error_codes": ERROR_CODES,
        }
        self._patterns = {k: re.compile(r["pattern"]) for k, r in ATTRIBUTES.items() if "pattern" in r}

    def span_class(self, name: object) -> str | None:
        """`http`, `ws`, `job`, `agent.call` or a child span's own name for a listed span name; None for
        anything else."""
        if not isinstance(name, str):
            return None
        if name == "agent.call" or name in CHILD_SPANS:
            return name
        head, sep, rest = name.partition(" ")
        if not sep:
            return None
        if head == "job" and rest in self._enums["job_kinds"]:
            return "job"
        if rest not in self._enums["route_templates"]:
            return None
        if head == "WS":
            return "ws"
        return "http" if head in METHODS else None

    def valid(self, key: str, value: object) -> bool:
        rule = ATTRIBUTES.get(key)
        if rule is None:
            return False
        kind = rule["type"]
        if kind == "string":
            if not isinstance(value, str) or len(value) > MAX_STRING:
                return False
            if "enum" in rule:
                return value in rule["enum"]
            if "enum_from" in rule:
                return value in self._enums[rule["enum_from"]]
            return bool(self._patterns[key].fullmatch(value))
        if kind == "int":
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif kind == "float":
            ok = isinstance(value, float)
        else:
            ok = isinstance(value, bool)
        if not ok:
            return False
        if "range" in rule:
            low, high = rule["range"]
            return bool(low <= value <= high)
        return True

    def clean(self, span_class: str | None, attributes: Mapping[str, object]) -> dict[str, object]:
        """Only the keys listed for `span_class` whose values pass their rule."""
        allowed = KEYS_BY_SPAN.get(span_class or "", ())
        return {k: v for k, v in attributes.items() if k in allowed and self.valid(k, v)}
