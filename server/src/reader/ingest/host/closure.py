"""The converter's import closure, computed statically (SRV-INGEST FR-INGEST-3).

One function serves both the setup record's `converter_source_sha256` and the unit test that every third-party
module the converter imports is pinned in requirements.lock.
"""

from __future__ import annotations

import ast
import hashlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

SRC: Final = Path(__file__).resolve().parents[3]  # .../src (the directory holding the `reader` package)
ROOT_MODULE: Final = "reader.ingest.host.convert"


@dataclass
class Closure:
    files: list[Path] = field(default_factory=list)
    third_party: set[str] = field(default_factory=set)


def _module_file(module: str, src: Path) -> Path | None:
    base = src.joinpath(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _imports(path: Path, module: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base = ".".join(parts[: len(parts) - node.level + 1])
                target = f"{base}.{node.module}" if node.module else base
            else:
                target = node.module or ""
            found.add(target)
            for alias in node.names:  # `from package import submodule`
                found.add(f"{target}.{alias.name}")
    return found


def import_closure(module: str = ROOT_MODULE, src: Path = SRC) -> Closure:
    closure = Closure()
    seen: set[str] = set()
    pending = [module]
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        top = name.split(".")[0]
        if top == "reader":
            path = _module_file(name, src)
            if path is None:
                continue  # `from reader.x import Name`: the name is not a module
            # a package's __init__ runs whenever any submodule is imported
            parts = name.split(".")
            for depth in range(2, len(parts)):
                pending.append(".".join(parts[:depth]))
            if path not in closure.files:
                closure.files.append(path)
            pending.extend(_imports(path, name))
        elif top and top not in sys.stdlib_module_names and top != "__future__":
            closure.third_party.add(top)
    closure.files.sort()
    return closure


def source_sha256(src: Path = SRC) -> str:
    """sha256 over the sorted (relative path, file sha256) pairs of the closure's `reader` files."""
    lines = []
    for path in import_closure(src=src).files:
        rel = path.relative_to(src).as_posix()
        lines.append(f"{rel} {hashlib.sha256(path.read_bytes()).hexdigest()}\n")
    return hashlib.sha256("".join(sorted(lines)).encode("utf-8")).hexdigest()
