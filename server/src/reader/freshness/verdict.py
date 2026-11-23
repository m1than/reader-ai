"""Stage (c), the report (FR-FRESH-8, VEC-FRESH-1..3, INV-FRESH-2/-3): the prompt, the strict parse and the
resolution of ids to R entries.

The prompt shows each search's R entries as `r<k> <host> <title>` (no URL) and its summary inside the web
markers, with the marker brackets replaced in every inserted string. The output's strings are sanitised and
cut; every cited or recommended id is resolved to its R entry, whose URL string, host and title are stored
(the model never writes a URL or a title); unknown ids, sections and claims are dropped and counted.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final

from reader.freshness import params
from reader.freshness.errors import OutputInvalid
from reader.freshness.links import Entry
from reader.freshness.sample import Topic
from reader.freshness.scan import escape, load_json
from reader.freshness.text import sanitise

WEB_OPEN: Final = "⟦web⟧"
WEB_CLOSE: Final = "⟦/web⟧"
VERDICTS: Final = frozenset({"current", "partly_outdated", "outdated"})
STATUSES: Final = frozenset({"changed", "unchanged", "unknown"})
REC_KINDS: Final = frozenset({"book_edition", "book", "official_docs", "course", "other"})
TOP_KEYS: Final = frozenset({"verdict", "reasons", "claims", "recommendations"})
REASON_KEYS: Final = frozenset({"text", "citations"})
CLAIM_KEYS: Final = frozenset({"id", "status", "note", "citations"})
REC_REQUIRED: Final = frozenset({"source", "kind", "why", "covers"})
REC_KEYS: Final = REC_REQUIRED | {"date"}
_DATE: Final = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")


@dataclass(frozen=True)
class SearchView:
    """One search as the report prompt shows it (memory only)."""

    query: str
    entries: tuple[Entry, ...]
    summary: str


@dataclass(frozen=True)
class Resolved:
    verdict: str
    reasons: list[dict[str, Any]]
    claim_status: list[dict[str, Any]]
    recommendations: list[dict[str, Any]]
    dropped: Mapping[str, int] = field(default_factory=dict)


def render(
    bibliography: Mapping[str, Any],
    topics: Sequence[Topic],
    claims: Sequence[Mapping[str, Any]],
    searches: Sequence[SearchView],
    today: date,
) -> str:
    bib = escape(json.dumps(dict(bibliography), ensure_ascii=False))
    lines = [f"Today (UTC): {today.isoformat()}", "", "Bibliography:", bib, "", "Topics (id: title):"]
    lines += [f"{t.id}: {escape(t.title)}" for t in topics]
    lines += ["", "Flagged statements (id, kind, confidence, page: subject):"]
    for c in claims:
        lines.append(f"{c['id']}, {c['kind']}, {c['confidence']}, p.{c['page']}: {escape(str(c['subject']))}")
    for n, search in enumerate(searches, start=1):
        lines += ["", f"{WEB_OPEN} search {n}", f"query: {escape(search.query)}"]
        lines += [f"{e.id} {escape(e.host)} {escape(e.title)}" for e in search.entries]
        lines += [f"summary: {escape(search.summary)}", WEB_CLOSE]
    return "\n".join(lines)


def _ids(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise OutputInvalid
    return list(value)


def _check_reason(reason: object) -> None:
    if not isinstance(reason, dict) or set(reason) != REASON_KEYS or not isinstance(reason["text"], str):
        raise OutputInvalid
    _ids(reason["citations"])


def _check_claim(claim: object) -> None:
    if not isinstance(claim, dict) or set(claim) != CLAIM_KEYS:
        raise OutputInvalid
    if not isinstance(claim["id"], str) or not isinstance(claim["note"], str):
        raise OutputInvalid
    if not _member(claim["status"], STATUSES):
        raise OutputInvalid
    _ids(claim["citations"])


def _member(value: object, allowed: frozenset[str]) -> bool:
    """An enum check that never hashes a list or a dict (a TypeError would escape as a crash)."""
    return isinstance(value, str) and value in allowed


def _check_recommendation(rec: object) -> None:
    if not isinstance(rec, dict) or not REC_REQUIRED <= set(rec) <= REC_KEYS:
        raise OutputInvalid
    if not isinstance(rec["source"], str) or not isinstance(rec["why"], str):
        raise OutputInvalid
    if not _member(rec["kind"], REC_KINDS):
        raise OutputInvalid
    if rec.get("date") is not None and not isinstance(rec["date"], str):
        raise OutputInvalid
    covers = rec["covers"]
    if not isinstance(covers, list) or not all(type(c) is int for c in covers):
        raise OutputInvalid


def parse(text: str) -> dict[str, Any]:
    """The output's schema, strictly: unknown keys, a wrong enum or type, or a missing key is
    `output_invalid`."""
    data = load_json(text)
    if not isinstance(data, dict) or set(data) != TOP_KEYS or not _member(data["verdict"], VERDICTS):
        raise OutputInvalid
    for key in ("reasons", "claims", "recommendations"):
        if not isinstance(data[key], list):
            raise OutputInvalid
    for reason in data["reasons"]:
        _check_reason(reason)
    for claim in data["claims"]:
        _check_claim(claim)
    for rec in data["recommendations"]:
        _check_recommendation(rec)
    return data


def valid_date(value: str, today: date) -> bool:
    """`YYYY`, `YYYY-MM` or `YYYY-MM-DD`, a real date, not after today."""
    found = _DATE.match(value)
    if found is None:
        return False
    year, month, day = found.group(1), found.group(2), found.group(3)
    try:
        if day is not None:
            return date(int(year), int(month), int(day)) <= today
        if month is not None:
            m = int(month)
            return 1 <= m <= 12 and (int(year), m) <= (today.year, today.month)
        return int(year) <= today.year
    except ValueError:
        return False


def resolve(
    data: Mapping[str, Any],
    entries: Mapping[str, Entry],
    claim_ids: set[str],
    section_ids: set[int],
    today: date,
) -> Resolved:
    dropped: dict[str, int] = {}

    def drop(reason: str, n: int = 1) -> None:
        dropped[reason] = dropped.get(reason, 0) + n

    def cite(ids: Sequence[str]) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        for rid in ids:
            entry = entries.get(rid)
            if entry is None:
                drop("source_unknown")
            elif rid not in seen:
                seen.add(rid)
                out.append(entry.citation())
        return out

    reasons = [
        {"text": sanitise(r["text"], params.TEXT_CHARS_MAX), "citations": cite(r["citations"])}
        for r in data["reasons"][: params.REASONS_MAX]
    ]
    if len(data["reasons"]) > params.REASONS_MAX:
        drop("reasons_over_max", len(data["reasons"]) - params.REASONS_MAX)
    statuses: list[dict[str, Any]] = []
    seen_claims: set[str] = set()
    for claim in data["claims"]:
        if claim["id"] not in claim_ids or claim["id"] in seen_claims:
            drop("claim_unknown")
            continue
        seen_claims.add(claim["id"])
        note = sanitise(claim["note"], params.TEXT_CHARS_MAX)
        citations = cite(claim["citations"])
        statuses.append({"id": claim["id"], "status": claim["status"], "note": note, "citations": citations})
    recommendations: list[dict[str, Any]] = []
    for rec in data["recommendations"]:
        entry = entries.get(rec["source"])
        if entry is None:
            drop("source_unknown")
            continue
        if len(recommendations) == params.RECOMMENDATIONS_MAX:
            drop("recommendations_over_max")
            continue
        kept_date = None
        if rec.get("date") is not None:
            candidate = sanitise(rec["date"], 10)
            if valid_date(candidate, today):
                kept_date = candidate
            else:
                drop("date_invalid")
        covers = [c for c in rec["covers"] if c in section_ids]
        if len(covers) < len(rec["covers"]):
            drop("section_unknown", len(rec["covers"]) - len(covers))
        recommendations.append(
            {
                **entry.citation(),
                "date": kept_date,
                "date_source": "model",
                "kind": rec["kind"],
                "why": sanitise(rec["why"], params.TEXT_CHARS_MAX),
                "covers": list(dict.fromkeys(covers)),
            }
        )
    if data["verdict"] != "current" and len(recommendations) < params.RECOMMENDATIONS_MIN:
        drop("recommendations_short", params.RECOMMENDATIONS_MIN - len(recommendations))
    return Resolved(data["verdict"], reasons, statuses, recommendations, dropped)
