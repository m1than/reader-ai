"""Report schemas, forced verdicts and citation expansion (SRV-PIPELINE FR-PIPELINE-4, FR-PIPELINE-7).

A fact report has exactly one verdict per claim key: (cell, claim) for `explain` claims, (cell, None) for `changed`
and `erratum` cells. A `supported` verdict without a span, or with an alias not in the source, is forced to
`unsupported` after validation. Anything else malformed is `output_invalid` (transient: the step is retried).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from reader.agent.errors import agent_error
from reader.pipeline.source import Span

ITEMS_MAX: Final = 400  # bounds.items_max_per_report
VERDICTS: Final = frozenset({"supported", "unsupported", "contradicted"})
TITLE_MAX: Final = 200

Key = tuple[str, str | None]


@dataclass(frozen=True)
class Verdict:
    cell: str
    claim: str | None
    verdict: str
    spans: tuple[str, ...]
    note: str
    forced: str | None  # the reason a `supported` verdict was forced to `unsupported`


@dataclass(frozen=True)
class Finding:
    cell: str | None
    claim: str | None
    blocking: bool
    text: str


def invalid() -> Exception:
    return agent_error("output_invalid")


def parse_json(text: str) -> Any:
    """The model's answer as JSON; one surrounding Markdown code fence is tolerated."""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.rsplit("```", 1)[0]
    try:
        return json.loads(body)
    except ValueError, RecursionError:
        raise invalid() from None


def claim_keys(notebook: Mapping[str, Any]) -> list[Key]:
    keys: list[Key] = []
    for cell in notebook.get("cells", []):
        reader = cell.get("metadata", {}).get("reader", {})
        if reader.get("kind") == "explain":
            keys.extend((str(cell["id"]), str(c["id"])) for c in reader.get("claims", []))
        elif reader.get("kind") in ("changed", "erratum"):
            keys.append((str(cell["id"]), None))
    return keys


def _opt_str(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise invalid()


def validate_fact_report(
    raw: Any,
    notebook: Mapping[str, Any],
    aliases: Mapping[str, Span],
    stripped: frozenset[Key] | set[Key] = frozenset(),
) -> list[Verdict]:
    items = raw.get("verdicts") if isinstance(raw, dict) else None
    if not isinstance(items, list) or len(items) > ITEMS_MAX:
        raise invalid()
    expected = claim_keys(notebook)
    seen: dict[Key, Verdict] = {}
    for item in items:
        if not isinstance(item, dict):
            raise invalid()
        cell, claim = item.get("cell"), _opt_str(item.get("claim"))
        verdict, spans, note = item.get("verdict"), item.get("spans", []), item.get("note", "")
        if not isinstance(cell, str) or verdict not in VERDICTS or not isinstance(note, str):
            raise invalid()
        if not isinstance(spans, list) or not all(isinstance(s, str) for s in spans):
            raise invalid()
        key: Key = (cell, claim)
        if key not in expected or key in seen:
            raise invalid()
        known = tuple(aliases[s].span_id for s in spans if s in aliases)
        forced = None
        if verdict == "supported":
            if len(known) < len(spans) or key in stripped:
                forced = "citation_unknown_span"
            elif not spans:
                forced = "no_span"
        seen[key] = Verdict(cell, claim, "unsupported" if forced else str(verdict), known, note, forced)
    if set(seen) != set(expected):
        raise invalid()
    return [seen[k] for k in expected]


def validate_review_report(raw: Any) -> list[Finding]:
    items = raw.get("findings") if isinstance(raw, dict) else None
    if not isinstance(items, list) or len(items) > ITEMS_MAX:
        raise invalid()
    out: list[Finding] = []
    for item in items:
        if not isinstance(item, dict):
            raise invalid()
        text, blocking = item.get("text"), item.get("blocking")
        if not isinstance(text, str) or not text.strip() or type(blocking) is not bool:
            raise invalid()
        out.append(Finding(_opt_str(item.get("cell")), _opt_str(item.get("claim")), blocking, text.strip()))
    return out


def expand_citations(
    citations: Sequence[Any], aliases: Mapping[str, Span], material_title: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Aliases to span ids with page and title from the span; unknown aliases removed (FR-PIPELINE-7)."""
    kept: list[dict[str, Any]] = []
    stripped: list[str] = []
    for citation in citations:
        if not isinstance(citation, dict):
            raise invalid()
        if "span" in citation:
            alias = citation["span"]
            span = aliases.get(alias) if isinstance(alias, str) else None
            if span is None:
                stripped.append(str(alias))
                continue
            kept.append({"span": span.span_id, "page": span.page, "material_title": material_title[:TITLE_MAX]})
        elif set(citation) == {"url", "retrieved"}:
            kept.append({"url": citation["url"], "retrieved": citation["retrieved"]})
        else:
            raise invalid()
    return kept, stripped
