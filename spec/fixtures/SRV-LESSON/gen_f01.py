# /// script
# requires-python = ">=3.14"
# dependencies = ["nbformat==5.11.1", "jsonschema==4.26.0"]
# ///
"""Generator of VEC-LESSON-2: the synthetic fixture lesson F01 (one cell of every ADR-0004 kind).

Run: `uv run --script spec/fixtures/SRV-LESSON/gen_f01.py` — writes F01.ipynb next to this file in the canonical form
of FR-LESSON-2 (`nbformat.writes`, UTF-8, no trailing newline), after validating it against nbformat and
cell.schema.json (with the format checker), checking each kind's
cell type, and running the lab's asserts. It also writes the byte-identical packaged copies the server ships
(server/src/reader/lessons/fixtures/F01.ipynb and server/src/reader/lessons/cell.schema.json; VEC-LESSON-1/2).
Every text is written for this fixture; nothing is quoted from any book.
The lab implements the catch-up formula of Nakamoto (2008), section 11; its expected values are that paper's table
(q = 0.1, z = 5: 0.0009137; q = 0.3, z = 5: 0.1773523) and ADR-0009's recomputed 62.8 % for q = 0.3, z = 1.
"""

import hashlib
import json
from pathlib import Path

import jsonschema
import nbformat
from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook

HERE = Path(__file__).resolve().parent
PACKAGE = HERE.parents[2] / "server" / "src" / "reader" / "lessons"
MATERIAL = hashlib.sha256(b"reader-ai synthetic material F01").hexdigest()
SPAN_1 = f"{MATERIAL}:1"   # span id form `<material_sha256>:<seq>` (cell.schema.json span_ref)
SPAN_7 = f"{MATERIAL}:7"
MATERIAL_TITLE = "Synthetic material F01"
CITE_PAPER = {"url": "https://bitcoin.org/bitcoin.pdf", "retrieved": "2026-11-21"}

LAB = '''import math


def attacker_success(q: float, z: int) -> float:
    """Probability that an attacker with hash share q ever catches up from z blocks behind (Nakamoto 2008, section 11)."""
    p = 1.0 - q
    lam = z * (q / p)
    total = 1.0
    for k in range(z + 1):
        poisson = math.exp(-lam)
        for i in range(1, k + 1):
            poisson *= lam / i
        total -= poisson * (1.0 - (q / p) ** (z - k))
    return total


print(round(attacker_success(0.1, 5), 7), round(attacker_success(0.3, 5), 7))
'''

ASSERTS = [
    "round(attacker_success(0.1, 5), 7) == 0.0009137",
    "round(attacker_success(0.3, 5), 7) == 0.1773523",
    "round(attacker_success(0.3, 1), 3) == 0.628",
    "attacker_success(0.1, 0) == 1.0",
]


def reader(kind: str, stage: str = "fixture", **fields: object) -> dict[str, object]:
    return {"reader": {"kind": kind, "sectors": ["consensus"], "provenance": {"stage": stage}, **fields}}


def build() -> nbformat.NotebookNode:
    md = new_markdown_cell
    cells = [
        md("## Before you start\n\nAnswer two questions to choose your route.", id="f01-diagnostic", metadata=reader(
            "diagnostic",
            questions=[
                {"id": "q1", "text": "Can you explain why a longer chain wins?"},
                {"id": "q2", "text": "Have you computed a Poisson tail before?"},
            ],
            routes={"q1-yes": ["explain-attack"], "q1-no": ["route-basics", "explain-attack"]})),
        md("## Reading route\n\nRead the synthetic section 1, then section 2.", id="f01-route", metadata=reader(
            "route",
            source_refs=[
                {"material_sha256": MATERIAL, "page": 1, "span": SPAN_1},
                {"material_sha256": MATERIAL, "page": 2},
            ])),
        md("## Catching up\n\nAn attacker who is z blocks behind catches up with a probability that falls exponentially in"
           " z while q < 0.5.", id="f01-explain", metadata=reader(
            "explain",
            claims=[{"id": "c1", "text": "The catch-up probability falls exponentially in z while q < 0.5.",
                     "citations": [CITE_PAPER, {"span": SPAN_1, "page": 1, "material_title": MATERIAL_TITLE}]}])),
        md("## What changed\n\nA synthetic note that a parameter changed after the material was written.",
           id="f01-changed", metadata=reader(
            "changed", as_of="2026-11-21",
            citations=[{"url": "https://example.org/fixture-change", "retrieved": "2026-11-21"}])),
        md("## Erratum\n\nThe synthetic material rounds 0.6277 to 63.4%; the correct value is 62.8%.",
           id="f01-erratum", metadata=reader(
            "erratum",
            source_ref={"material_sha256": MATERIAL, "page": 2, "span": SPAN_7},
            correction="q = 0.3, z = 1 gives 0.628, not 0.634.",
            citations=[CITE_PAPER])),
        md("## Trade-off\n\n| Confirmations | Wait | Risk at q = 0.1 |\n| --- | --- | --- |\n| 1 | short | 0.2046 |\n"
           "| 6 | long | 0.0002 |", id="f01-tradeoff", metadata=reader(
            "tradeoff",
            options=[{"id": "z1", "name": "One confirmation"}, {"id": "z6", "name": "Six confirmations"}],
            rejected_because={"z1": "Risk 0.2046 at q = 0.1 is too high for large payments."})),
        new_code_cell(LAB, id="f01-lab", metadata=reader(
            "lab", kernel="python3", network="none", asserts=ASSERTS,
            verified={"runs": 2, "passed": True, "outputs_identical": True,
                      "input_sha256": hashlib.sha256(LAB.encode()).hexdigest()})),
        md("**Recall:** what is the catch-up probability for q = 0.1, z = 5?", id="f01-recall", metadata=reader(
            "recall", front="Catch-up probability for q = 0.1, z = 5?", back="0.0009137")),
        md("## Your notes\n\n", id="f01-notes", metadata=reader("notes", stage="template")),
        md("## Open question\n\nDoes the model hold when block times vary? (non-blocking review finding)",
           id="f01-open", metadata=reader(
            "open", status="open", answer_ref=None,
            finding={"stage": "adversarial_review", "blocking": False,
                     "text": "The analysis assumes a fixed block rate."})),
    ]
    nb = new_notebook(cells=cells, metadata={
        "kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"},
        "language_info": {"name": "python"},
        "reader": {"lesson_format": 1, "title": "F01 — fixture lesson (synthetic)"},
    })
    nb.nbformat_minor = 5
    return nb


def check(nb: nbformat.NotebookNode) -> None:
    nbformat.validate(nb)
    schema = json.loads((HERE / "cell.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER)
    for cell in nb.cells:
        meta = cell.metadata["reader"]
        validator.validate(meta)
        assert schema["x-cell-type"][meta["kind"]] == cell.cell_type, cell.id
    assert {c.metadata["reader"]["kind"] for c in nb.cells} == set(schema["x-cell-type"]), "one cell of every kind"
    namespace: dict[str, object] = {}
    exec(LAB, namespace)  # noqa: S102 — the fixture's own lab code, written above
    for expression in ASSERTS:
        assert eval(expression, namespace), expression  # noqa: S307 — the fixture's own asserts


if __name__ == "__main__":
    notebook = build()
    check(notebook)
    data = nbformat.writes(notebook).encode("utf-8")
    (HERE / "F01.ipynb").write_bytes(data)
    (PACKAGE / "fixtures").mkdir(parents=True, exist_ok=True)
    (PACKAGE / "fixtures" / "F01.ipynb").write_bytes(data)
    (PACKAGE / "cell.schema.json").write_bytes((HERE / "cell.schema.json").read_bytes())
