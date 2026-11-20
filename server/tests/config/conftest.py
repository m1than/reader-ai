"""Shared helpers for the SRV-CONFIG tests: fixture loading and small utilities.

The module under test (`reader.config`) is imported inside each test body, never here, so that in the Red
phase every test fails on its own (spec/modules/SRV-CONFIG.yaml, header note).
"""

import os
import traceback
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

FIXTURES = Path(__file__).resolve().parents[3] / "spec" / "fixtures" / "SRV-CONFIG"
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
skip_as_root = pytest.mark.skipif(IS_ROOT, reason="permissions do not apply to uid 0")


def load_fixture(name: str) -> Any:
    """Read a spec fixture with a plain YAML 1.2 safe loader (the fixtures are trusted test data)."""
    return YAML(typ="safe", pure=True).load((FIXTURES / name).read_text(encoding="utf-8"))


def write_config(directory: Path, text: str, name: str = "reader.yaml") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def rendered(error: BaseException) -> str:
    """Every textual surface of an exception that a log or a crash report could show."""
    return "\n".join([
        str(error),
        repr(error),
        "".join(traceback.format_exception(error)),
    ])


def rendered_with_locals(error: BaseException) -> str:
    return "".join(traceback.TracebackException.from_exception(error, capture_locals=True).format())


def canaries(text: str) -> list[str]:
    """The CANARY-… tokens planted in a fixture text."""
    return sorted({word.strip("\"',{}:[]") for word in text.split() if "CANARY-" in word})
