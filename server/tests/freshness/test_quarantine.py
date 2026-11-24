# @trace INV-FRESH-5
"""[unit] Quarantine: no module outside `reader.freshness` imports it (the routes and jobs modules are named
only by SRV-AUTH's ROUTERS and SRV-EVENTS's KIND_MODULES, as strings), and none names its tables in SQL, so
web-derived text reaches no other prompt."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "reader"
TABLES = ("freshness_reports", "material_bibliography")
# The two registrations, each the one public entry point of its kind (INV-FRESH-5).
REGISTERED = {
    ("api/app.py", "reader.freshness.routes"),
    ("jobs/catalog.py", "reader.freshness.jobs"),
}


def _outside() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if "freshness" not in p.relative_to(SRC).parts]


def test_no_module_outside_imports_reader_freshness() -> None:
    offenders: list[str] = []
    for path in _outside():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            if any(n == "reader.freshness" or n.startswith("reader.freshness.") for n in names):
                offenders.append(str(path.relative_to(SRC)))
    assert offenders == []


def test_only_the_registrations_name_the_package() -> None:
    found: set[tuple[str, str]] = set()
    for path in _outside():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            value = node.value if isinstance(node, ast.Constant) else None
            if isinstance(value, str) and "reader.freshness" in value:
                found.add((path.relative_to(SRC).as_posix(), value))
    assert found == REGISTERED


def test_no_module_outside_names_the_tables() -> None:
    offenders = [
        str(path.relative_to(SRC))
        for path in _outside()
        if any(table in path.read_text(encoding="utf-8") for table in TABLES)
    ]
    assert offenders == []


def test_the_package_reads_no_other_prompt_input() -> None:
    """The package's own modules never import the pipeline's or the tutor's prompt builders (the other way
    round would carry web text into them; this way is the defer's future seam, kept closed for now)."""
    package = SRC / "freshness"
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(("reader.pipeline", "reader.tutor", "reader.lessons")), path
