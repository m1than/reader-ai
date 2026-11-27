"""Report schemas, forced verdicts and citation expansion (SRV-PIPELINE FR-PIPELINE-4, FR-PIPELINE-7).

A fact report has exactly one verdict per claim key: (cell, claim) for `explain` claims, (cell, None) for
`changed` and `erratum` cells. A `supported` verdict without a span, or with an alias not in the source, is
forced to `unsupported` after validation. Anything else malformed is `output_invalid` (transient: the
step is retried).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from reader.agent.errors import agent_error
from reader.ingest import locator
from reader.pipeline.source import Span, material_of

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
    credibility: int = 6  # Admiralty 1 to 6 after FR-PIPELINE-27's constraint


# FR-PIPELINE-27: the credibility values each verdict allows, and the value an out-of-set one becomes.
CREDIBILITY: Final[Mapping[str, tuple[frozenset[int], int]]] = {
    "supported": (frozenset({1, 2, 3}), 3),
    "unsupported": (frozenset({4, 6}), 6),
    "contradicted": (frozenset({5}), 5),
    "source_too_large": (frozenset({6}), 6),
}


def constrain_credibility(verdict: str, value: int, span_ids: Sequence[str]) -> int:
    """FR-PIPELINE-27 (VEC-PIPELINE-6): the model's value kept when its verdict allows it, else the verdict's
    default; 1 ("confirmed by independent sources") needs cited spans from two or more materials, else 2."""
    allowed, default = CREDIBILITY[verdict]
    out = value if value in allowed else default
    try:
        materials = {material_of(s) for s in span_ids}
    except locator.LocatorError:  # unreachable: every id came from a view (FR-PIPELINE-7)
        raise invalid() from None
    if out == 1 and len(materials) < 2:
        out = 2
    return out


def span_ref(span: Span) -> dict[str, str]:
    """FR-PIPELINE-7: the span id, plus its quote_sha256 when the id is a locator (a legacy id gets none).
    A locator without a stored hash cannot occur (an active pack is sealed); if it does, the step fails
    closed."""
    if locator.is_legacy_id(span.span_id):
        return {"span": span.span_id}
    if span.quote_sha256 is None:
        raise invalid()
    return {"span": span.span_id, "quote_sha256": span.quote_sha256}


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
        credibility = item.get("credibility")
        if type(credibility) is not int or not 1 <= credibility <= 6:  # FR-PIPELINE-4: required, 1 to 6
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
        final = "unsupported" if forced else str(verdict)
        seen[key] = Verdict(
            cell, claim, final, known, note, forced, constrain_credibility(final, credibility, known)
        )
    if set(seen) != set(expected):
        raise invalid()
    return [seen[k] for k in expected]


def check_failed(stage: str, report: Mapping[str, Any]) -> bool:
    """FR-PIPELINE-4: each check's outcome from its stored report. `source_too_large` does not fail."""
    if stage == "rules":
        return bool(report.get("failures"))
    if stage == "lab_verification":
        results = (report.get("lab_report") or {}).get("results", {})
        problems = (report.get("lab_report") or {}).get("problems", [])
        return bool(problems) or any(not r.get("passed") for r in results.values())
    if stage == "fact_check":
        return any(v["verdict"] in ("unsupported", "contradicted") for v in report.get("verdicts", []))
    if stage == "adversarial_review":
        return any(f["blocking"] for f in report.get("findings", []))
    return False


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
            kept.append({**span_ref(span), "page": span.page, "material_title": material_title[:TITLE_MAX]})
        elif set(citation) == {"url", "retrieved"}:
            kept.append({"url": citation["url"], "retrieved": citation["retrieved"]})
        else:
            raise invalid()
    return kept, stripped
