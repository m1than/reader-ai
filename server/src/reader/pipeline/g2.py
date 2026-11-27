"""The G2 report: what publish would check (FR-PIPELINE-8, VEC-PIPELINE-3). No publish in the slice.

Computed by the findings step, the run's last step. Failing checks, in report order: labs (binding,
unverified), claims (uncited, unsupported, blocking), stages (a check with no step on the last model-written
revision M's deterministic derivation), deltas (the final revision is not
`append_findings(stamp_credibility(stamp_labs(M, lab_report), fact_report), problems)`).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any, Final

from reader.lessons.schema import LessonWriteError, validate
from reader.pipeline import notebook as nb_mod
from reader.pipeline.findings import (
    Problem,
    append_findings,
    labs_of,
    labs_sha256,
    source_sha256,
    stamp_credibility,
    stamp_labs,
)
from reader.pipeline.reports import Key, Verdict
from reader.pipeline.verify import LabReport

CHECKS: Final = ("rules", "lab_verification", "fact_check", "adversarial_review")


def _canonical(notebook: Mapping[str, Any]) -> bytes:
    """FR-PIPELINE-8 (d): the canonical form, SRV-LESSON's canonical bytes (FR-LESSON-2), so nbformat's
    split of an appended cell's source into lines is not a delta."""
    return validate(nb_mod.to_bytes(notebook)).canonical


def canonical_sha256(notebook: Mapping[str, Any]) -> str | None:
    """The revision sha256 SRV-LESSON gives this notebook, or None when it has no canonical form."""
    try:
        return hashlib.sha256(_canonical(notebook)).hexdigest()
    except (LessonWriteError, UnicodeEncodeError):
        return None


def derive(
    model_written: Mapping[str, Any], lab_report: LabReport | None, verdicts: Sequence[Verdict] | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(stamp_labs(M), stamp_credibility(stamp_labs(M))), with an absent report as the identity."""
    labbed = stamp_labs(model_written, lab_report) if lab_report is not None else dict(model_written)
    credited = stamp_credibility(labbed, verdicts) if verdicts is not None else labbed
    return labbed, credited


def deltas_ok(
    final: Mapping[str, Any],
    model_written: Mapping[str, Any],
    problems: Sequence[Problem],
    lab_report: LabReport | None,
    verdicts: Sequence[Verdict] | None = None,
) -> bool:
    """False also when either notebook has no canonical form: `deltas.mismatch`, never a failed step."""
    expected = append_findings(derive(model_written, lab_report, verdicts)[1], problems)
    try:
        return _canonical(final) == _canonical(expected)
    except (LessonWriteError, UnicodeEncodeError):
        return False


def _verified(cell: Mapping[str, Any]) -> bool:
    v = cell["metadata"]["reader"].get("verified")
    return (
        isinstance(v, dict)
        and v.get("passed") is True
        and v.get("outputs_identical") is True
        and v.get("input_sha256") == source_sha256(cell)
    )


def report(
    final: Mapping[str, Any],
    missing: Sequence[str],
    lab_report: LabReport | None,
    verdicts: Mapping[Key, str],
    deltas: bool,
) -> dict[str, Any]:
    failing: list[str] = []
    if lab_report is not None and lab_report.labs_sha256 != labs_sha256(final):
        failing.append("labs.binding")
    if not all(_verified(cell) for _, cell in labs_of(final)):
        failing.append("labs.unverified")
    uncited = unsupported = blocking = False
    for cell in final.get("cells", []):
        reader = cell.get("metadata", {}).get("reader", {})
        kind = reader.get("kind")
        if kind == "explain":
            claims = reader.get("claims", [])
            uncited = uncited or not claims or any(not c.get("citations") for c in claims)
        if kind in ("changed", "erratum") and verdicts.get((cell["id"], None)) != "supported":
            unsupported = True
        finding = reader.get("finding")
        if (
            kind == "open"
            and isinstance(finding, dict)
            and finding.get("blocking")
            and reader.get("status") != ("answered")
        ):
            blocking = True  # `wontfix` still counts as blocking
    failing += [
        name
        for name, bad in (
            ("claims.uncited", uncited),
            ("claims.unsupported", unsupported),
            ("claims.blocking", blocking),
        )
        if bad
    ]
    failing += [f"stages.missing_{s}" for s in CHECKS if s in missing]
    if not deltas:
        failing.append("deltas.mismatch")
    return {"publishable": not failing, "failing": failing, "labs_sha256": labs_sha256(final)}
