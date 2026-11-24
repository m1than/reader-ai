"""The three versioned prompt files (FR-FRESH-3, -7, -8): `prompts/<name>.<version>.md`, with their sha256."""

from __future__ import annotations

import functools
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

PROMPTS_DIR: Final = Path(__file__).resolve().parent / "prompts"
VERSIONS: Final = {"scan": "v1", "search": "v1", "report": "v1"}
# The report stage's file is `verdict.<version>.md` (the spec's FR-FRESH-8 names it so).
FILES: Final = {"scan": "scan", "search": "search", "report": "verdict"}


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    text: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def record(self) -> dict[str, str]:
        return {"version": self.version, "sha256": self.sha256}


@functools.cache
def load(name: str) -> Prompt:
    version = VERSIONS[name]
    return Prompt(name, version, (PROMPTS_DIR / f"{FILES[name]}.{version}.md").read_text(encoding="utf-8"))
