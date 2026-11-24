"""Helpers for the settings overlay tests (SRV-CONFIG FR-CONFIG-25/-26), under a module name of their own: the
`conftest` module name is shared with other test directories, so these tests import nothing from it."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[3] / "spec" / "fixtures" / "SRV-CONFIG"


@dataclass(frozen=True)
class Pg:
    host: str
    port: int
    user: str
    password: str
    database: str


def write_config(directory: Path, text: str, name: str = "reader.yaml") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path
