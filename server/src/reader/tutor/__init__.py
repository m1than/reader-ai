"""The tutor (SRV-TUTOR): questions about the open lesson, answered by one model call from retrieved spans."""

from __future__ import annotations

from pathlib import Path
from typing import Final

MIGRATIONS: Final = Path(__file__).resolve().parent / "migrations"
