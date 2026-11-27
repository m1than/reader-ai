"""The rule check: the first check after drafting, 0 model calls (FR-PIPELINE-24, -25; VEC-PIPELINE-7).

Each rule is a pure function over the stored notebook (span ids, not aliases), the lesson's source spans, the
model step's citation strip list and the declared reference libraries. The rules are named in stages.yaml
`rules` and run in that order; the report lists failures in cell order, then rule order. Failure texts are
fixed sentences with ids only, never book or model text.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from reader.pipeline.source import Span

RULES_VERSION: Final = "1"
ITEMS_MAX: Final = 400  # bounds.items_max_per_report

Key = tuple[str, str | None]


@dataclass(frozen=True)
class RuleInput:
    notebook: Mapping[str, Any]
    spans: Mapping[str, Span]  # span id → span of the lesson's source
    stripped: Collection[Key]  # (cell, claim) whose citations named an alias not in the source
    libraries: Collection[str]


@dataclass(frozen=True)
class Failure:
    rule: str
    cell: str | None
    claim: str | None
    text: str

    def as_json(self) -> dict[str, Any]:
        return {"rule": self.rule, "cell": self.cell, "claim": self.claim, "text": self.text}


def _cells(notebook: Mapping[str, Any]) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    out = []
    for cell in notebook.get("cells", []):
        reader = cell.get("metadata", {}).get("reader", {})
        out.append((str(cell["id"]), reader, cell))
    return out


def explain_without_claims(data: RuleInput) -> list[Failure]:
    return [
        Failure("explain_without_claims", cid, None, f"The explain cell {cid} states no claim.")
        for cid, reader, _ in _cells(data.notebook)
        if reader.get("kind") == "explain" and not reader.get("claims")
    ]


def claim_uncited(data: RuleInput) -> list[Failure]:
    return [
        Failure("claim_uncited", cid, str(claim["id"]), f"The claim {cid}/{claim['id']} cites no source.")
        for cid, reader, _ in _cells(data.notebook)
        if reader.get("kind") == "explain"
        for claim in reader.get("claims", [])
        if not claim.get("citations")
    ]


def cell_uncited(data: RuleInput) -> list[Failure]:
    return [
        Failure("cell_uncited", cid, None, f"The {reader['kind']} cell {cid} cites no source.")
        for cid, reader, _ in _cells(data.notebook)
        if reader.get("kind") in ("changed", "erratum") and not reader.get("citations")
    ]


def citation_stripped(data: RuleInput) -> list[Failure]:
    out = []
    for cell, claim in sorted(data.stripped, key=lambda k: (k[0], k[1] or "")):
        where = f"{cell}/{claim}" if claim else cell
        out.append(
            Failure("citation_stripped", cell, claim, f"{where} cited a source span that does not exist.")
        )
    return out


def _labs(notebook: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [(cid, reader) for cid, reader, _ in _cells(notebook) if reader.get("kind") == "lab"]


def lab_reference_missing(data: RuleInput) -> list[Failure]:
    return [
        Failure("lab_reference_missing", cid, None, f"The lab {cid} declares no independent reference.")
        for cid, reader in _labs(data.notebook)
        if not isinstance(reader.get("reference"), dict)
    ]


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _named(module: str, asserts: Sequence[str]) -> bool:
    pattern = re.compile(r"(?<![A-Za-z0-9_.])" + re.escape(module) + r"(?![A-Za-z0-9_])")
    return any(pattern.search(a) for a in asserts)


def reference_problem(reference: Mapping[str, Any], asserts: Sequence[str], data: RuleInput) -> str | None:
    """Why a declared reference does not resolve, or None (FR-PIPELINE-25)."""
    kind = reference.get("kind")
    if kind == "test_vector":
        span = data.spans.get(str(reference.get("span"))) if reference.get("span") else None
        if span is None:
            return "its test vector names no span of the source"
        text = _nfc(span.text)
        for expected in reference.get("expected", []):
            if _nfc(str(expected)) not in text:
                return "an expected value is not in the cited span"
            if not any(_nfc(str(expected)) in _nfc(a) for a in asserts):
                return "an expected value is in no assert"
        return None
    if kind == "library":
        module = str(reference.get("module", ""))
        if module not in data.libraries:
            return "its reference library is not declared"
        if not _named(module, asserts):
            return "its reference library is named in no assert"
        return None
    if kind == "second_implementation":
        return None
    return "its reference kind is unknown"


def lab_reference_unresolved(data: RuleInput) -> list[Failure]:
    out = []
    for cid, reader in _labs(data.notebook):
        reference = reader.get("reference")
        if not isinstance(reference, dict):
            continue  # lab_reference_missing reports it
        problem = reference_problem(reference, [str(a) for a in reader.get("asserts", [])], data)
        if problem is not None:
            out.append(Failure("lab_reference_unresolved", cid, None, f"The lab {cid}: {problem}."))
    return out


def labs_needed_s(notebook: Mapping[str, Any]) -> float:
    """FR-PIPELINE-9's time bound for the labs, in seconds."""
    from reader.pipeline.verify import INTERRUPT_GRACE_S, LAB_EXEC_TIMEOUT_S, LAB_RUNS, RESET_DEADLINE_S

    steps = sum(1 + len(reader.get("asserts", [])) for _, reader in _labs(notebook))
    if steps == 0:
        return 0.0
    return steps * (LAB_EXEC_TIMEOUT_S + INTERRUPT_GRACE_S) * LAB_RUNS + LAB_RUNS * RESET_DEADLINE_S


def labs_unverifiable(data: RuleInput) -> list[Failure]:
    from reader.pipeline.verify import VERIFY_JOB_TIMEOUT_S

    if labs_needed_s(data.notebook) <= VERIFY_JOB_TIMEOUT_S:
        return []
    text = "The labs need more time than one verification may take."
    return [Failure("labs_unverifiable", None, None, text)]


RULES: Final[Mapping[str, Callable[[RuleInput], list[Failure]]]] = {
    "explain_without_claims": explain_without_claims,
    "claim_uncited": claim_uncited,
    "cell_uncited": cell_uncited,
    "citation_stripped": citation_stripped,
    "lab_reference_missing": lab_reference_missing,
    "lab_reference_unresolved": lab_reference_unresolved,
    "labs_unverifiable": labs_unverifiable,
}


def check(names: Sequence[str], data: RuleInput) -> list[Failure]:
    """The declared rules in order; failures sorted by cell order, then rule order, capped."""
    position = {cid: i for i, (cid, _, _) in enumerate(_cells(data.notebook))}
    rank = {name: i for i, name in enumerate(names)}
    failures = [f for name in names for f in RULES[name](data)]
    failures.sort(key=lambda f: (position.get(f.cell or "", len(position)), rank[f.rule]))
    return failures[:ITEMS_MAX]


def report(names: Sequence[str], data: RuleInput) -> dict[str, Any]:
    return {"failures": [f.as_json() for f in check(names, data)]}
