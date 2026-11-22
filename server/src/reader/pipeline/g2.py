"""The G2 report: what publish would check (SRV-PIPELINE FR-PIPELINE-8, VEC-PIPELINE-3). No publish in the slice.

Failing checks, in report order: labs (binding, unverified), claims (uncited, unsupported, blocking), stages
(missing fact_check or adversarial_review record on the last model-written revision), deltas (the final revision is
not the deterministic function of the last model-written one).
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from reader.pipeline.findings import Problem, append_findings, labs_of, labs_sha256, source_sha256, stamp_labs
from reader.pipeline.reports import Key
from reader.pipeline.verify import LabReport


def _canonical(notebook: Mapping[str, Any]) -> str:
    return json.dumps(notebook, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def deltas_ok(
    final: Mapping[str, Any], model_written: Mapping[str, Any], problems: Sequence[Problem], lab_report: LabReport
) -> bool:
    expected = stamp_labs(append_findings(model_written, problems), lab_report)
    return _canonical(final) == _canonical(expected)


def _verified(cell: Mapping[str, Any]) -> bool:
    v = cell["metadata"]["reader"].get("verified")
    return (
        isinstance(v, dict) and v.get("passed") is True and v.get("outputs_identical") is True
        and v.get("input_sha256") == source_sha256(cell)
    )


def report(
    final: Mapping[str, Any],
    records: Collection[str],
    lab_report: LabReport,
    verdicts: Mapping[Key, str],
    deltas: bool,
) -> dict[str, Any]:
    failing: list[str] = []
    if lab_report.labs_sha256 != labs_sha256(final):
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
        if kind == "open" and isinstance(finding, dict) and finding.get("blocking") and reader.get("status") != (
            "answered"
        ):
            blocking = True  # `wontfix` still counts as blocking
    failing += [name for name, bad in (("claims.uncited", uncited), ("claims.unsupported", unsupported),
                                       ("claims.blocking", blocking)) if bad]
    failing += [f"stages.missing_{s}" for s in ("fact_check", "adversarial_review") if s not in records]
    if not deltas:
        failing.append("deltas.mismatch")
    return {"publishable": not failing, "failing": failing, "labs_sha256": labs_sha256(final)}
