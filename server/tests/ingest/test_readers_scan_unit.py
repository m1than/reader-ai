"""Every read of the ingest tables goes through the active view (SRV-INGEST INV-INGEST-16; ADR-0039 P5).
[unit]

@trace INV-INGEST-16

An AST scan: no string constant of a reader.* module outside reader.ingest names `ingests`, `material_spans`
or `material_outline` (docstrings excluded), except the foreign-key lines of two migrations.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "reader"
TABLES = re.compile(r"\b(ingests|material_spans|material_outline)\b")
FK = "ingest_id uuid NOT NULL REFERENCES ingests (id) ON DELETE RESTRICT"
ALLOWED = {("plan/migrations/plan_0001.py", FK), ("freshness/migrations/freshness_0001.py", FK)}


def _docstrings(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def offenders(root: Path) -> list[tuple[str, str]]:
    """(file, offending line) for every string constant naming an ingest table, outside reader/ingest."""
    out: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith("ingest/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _docstrings(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
                for line in node.value.splitlines():
                    if TABLES.search(line) and (rel, line.strip().rstrip(",")) not in ALLOWED:
                        out.append((rel, line.strip()))
    return out


def test_no_reader_outside_ingest_names_the_ingest_tables() -> None:
    assert offenders(SRC) == []


def test_the_allowed_references_are_the_only_exception_and_still_exist() -> None:
    for rel, line in ALLOWED:
        assert line in (SRC / rel).read_text(encoding="utf-8")


def test_a_planted_literal_is_found_and_a_docstring_is_not(tmp_path: Path) -> None:
    (tmp_path / "ingest").mkdir()
    (tmp_path / "ingest" / "store.py").write_text('SQL = "SELECT 1 FROM material_spans"\n', encoding="utf-8")
    (tmp_path / "planted").mkdir()
    (tmp_path / "planted" / "mod.py").write_text(
        '"""Reads material_spans in prose only."""\n\n'
        "def f() -> str:\n"
        '    """And ingests here."""\n'
        '    return "SELECT s.text FROM materials m JOIN ingests i ON i.material_id = m.id"\n',
        encoding="utf-8",
    )
    assert offenders(tmp_path) == [
        ("planted/mod.py", "SELECT s.text FROM materials m JOIN ingests i ON i.material_id = m.id")
    ]
