"""The agent runtime (SRV-AGENT): agent-proxy, the Provider port and its SDK adapter, the spend ledger."""

from __future__ import annotations

from pathlib import Path
from typing import Final

MIGRATIONS: Final = Path(__file__).resolve().parent / "migrations"
