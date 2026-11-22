"""The compile pipeline (SRV-PIPELINE): stages as data, one revise loop, findings as open cells, G2 report."""

from __future__ import annotations

from pathlib import Path
from typing import Final

MIGRATIONS: Final = Path(__file__).resolve().parent / "migrations"
