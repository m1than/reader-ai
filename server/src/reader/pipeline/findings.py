"""Open problems as `open` cells, lab stamps and the lab binding (SRV-PIPELINE FR-PIPELINE-8, -9, -10).

The findings step appends one `open` cell per distinct problem; its id is `f-` + 16 hex of sha256(kind,
cell, claim, text), so problems with the same id collapse (blocking if any is) and a rerun adds nothing.
`stamp_labs` writes lab verification's results into the lab cells and appends its failures. Both are pure
functions over the notebook dict, so G2 can recompute the final revision from the stored inputs
(INV-PIPELINE-5).
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from reader.pipeline.reports import Finding, Verdict
    from reader.pipeline.verify import LabReport

TEXT_MAX: Final = 20000


@dataclass(frozen=True)
class Problem:
    kind: str  # fact_check, adversarial_review, changed_unchecked, url_citation_unchecked, lab_failed, ...
    cell: str | None
    claim: str | None
    text: str
    stage: str  # finding.stage: fact_check, adversarial_review or lab_verification
    blocking: bool

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "cell": self.cell,
            "claim": self.claim,
            "text": self.text,
            "stage": self.stage,
            "blocking": self.blocking,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Problem:
        return cls(
            data["kind"], data["cell"], data["claim"], data["text"], data["stage"], bool(data["blocking"])
        )


def open_cell_id(problem: Problem) -> str:
    key = "\0".join((problem.kind, problem.cell or "", problem.claim or "", problem.text))
    return "f-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def slice_items(notebook: Mapping[str, Any]) -> list[Problem]:
    """The slice's open items: every `changed` cell, and URL citations (no web fact-check yet)."""
    items: list[Problem] = []
    urls = 0
    for cell in notebook.get("cells", []):
        reader = cell.get("metadata", {}).get("reader", {})
        citations = list(reader.get("citations", []))
        for claim in reader.get("claims", []):
            citations.extend(claim.get("citations", []))
        urls += sum(1 for c in citations if isinstance(c, dict) and "url" in c)
        if reader.get("kind") == "changed":
            text = "This change after the material was written is not web-checked yet."
            items.append(Problem("changed_unchecked", str(cell["id"]), None, text, "fact_check", False))
    if urls:
        text = f"{urls} URL citation(s) in this lesson are not web-checked yet."
        items.append(Problem("url_citation_unchecked", None, None, text, "fact_check", False))
    return items


def problems_from(
    verdicts: Sequence[Verdict], review: Sequence[Finding], notebook: Mapping[str, Any]
) -> list[Problem]:
    problems = [
        Problem(
            "fact_check",
            v.cell,
            v.claim,
            f"{v.verdict}: {v.cell}" + (f"/{v.claim}" if v.claim else "") + (f": {v.note}" if v.note else ""),
            "fact_check",
            True,
        )
        for v in verdicts
        if v.verdict in ("unsupported", "contradicted")
    ]
    problems += [
        Problem("adversarial_review", f.cell, f.claim, f.text, "adversarial_review", f.blocking)
        for f in review
    ]
    return problems + slice_items(notebook)


def _open_cell(cid: str, problem: Problem, blocking: bool, stage: str) -> dict[str, Any]:
    text = problem.text[:TEXT_MAX]
    return {
        "id": cid,
        "cell_type": "markdown",
        "metadata": {
            "reader": {
                "kind": "open",
                "sectors": [],
                "provenance": {"stage": stage},
                "status": "open",
                "answer_ref": None,
                "finding": {"stage": problem.stage, "blocking": blocking, "text": text},
            }
        },
        "source": f"## Open question\n\n{text}",
    }


def _same_finding(cell: Mapping[str, Any], problem: Problem) -> bool:
    reader = cell.get("metadata", {}).get("reader", {})
    finding = reader.get("finding")
    return (
        reader.get("kind") == "open"
        and isinstance(finding, dict)
        and finding.get("text") == problem.text[:TEXT_MAX]
        and finding.get("stage") == problem.stage
    )


def _append(notebook: Mapping[str, Any], problems: Sequence[Problem], stage: str) -> dict[str, Any]:
    out = copy.deepcopy(dict(notebook))
    cells = {c["id"]: c for c in out["cells"]}
    order: list[str] = []
    merged: dict[str, tuple[Problem, bool]] = {}
    for p in problems:
        base = open_cell_id(p)
        cid, n = base, 0
        while cid in cells and not _same_finding(cells[cid], p):  # a taken id: suffix, never drop
            n += 1
            cid = f"{base}-{n}"
        if cid in cells:  # this finding is already in the notebook (a rerun)
            continue
        if cid in merged:
            first, blocking = merged[cid]
            merged[cid] = (first, blocking or p.blocking)
        else:
            merged[cid] = (p, p.blocking)
            order.append(cid)
    out["cells"] = [*out["cells"], *(_open_cell(cid, *merged[cid], stage) for cid in order)]
    return out


def append_findings(notebook: Mapping[str, Any], problems: Sequence[Problem]) -> dict[str, Any]:
    return _append(notebook, problems, "findings")


def labs_of(notebook: Mapping[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    return [
        (i, cell)
        for i, cell in enumerate(notebook.get("cells", []))
        if cell.get("metadata", {}).get("reader", {}).get("kind") == "lab"
    ]


def source_text(cell: Mapping[str, Any]) -> str:
    source = cell.get("source", "")
    return "".join(source) if isinstance(source, list) else str(source)


def source_sha256(cell: Mapping[str, Any]) -> str:
    return hashlib.sha256(source_text(cell).encode("utf-8")).hexdigest()


def labs_sha256(notebook: Mapping[str, Any]) -> str:
    """The binding of G2 (a): the ordered labs as (cell index, id, source, asserts, kernel, network)."""
    labs = []
    for i, cell in labs_of(notebook):
        reader = cell["metadata"]["reader"]
        labs.append(
            [
                i,
                cell["id"],
                source_text(cell),
                reader.get("asserts"),
                reader.get("kernel"),
                reader.get("network"),
            ]
        )
    data = json.dumps(labs, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def stamp_labs(notebook: Mapping[str, Any], report: LabReport) -> dict[str, Any]:
    out = copy.deepcopy(dict(notebook))
    for _, cell in labs_of(out):
        result = report.results.get(cell["id"])
        if result is not None:
            cell["metadata"]["reader"]["verified"] = dict(result)
    return _append(out, report.problems, "lab_verification")
