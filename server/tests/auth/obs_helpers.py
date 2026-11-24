"""The SRV-OBS and SRV-PIPELINE test helpers the auth tests use, importable when an auth test file runs alone.

tests/auth is not a package (its files import `conftest` and `auth_helpers` from their own directory), so
pytest puts only tests/auth on sys.path; the `obs` and `pipeline` test packages live one level up. This module
adds tests/ once and re-exports what the auth tests need (phase 2 code review round 2, R2-2)."""

from __future__ import annotations

import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]
if str(TESTS) not in sys.path:
    sys.path.append(str(TESTS))

from obs.conftest import load_yaml, obs_setup  # noqa: E402
from pipeline.conftest import import_material, plan, synthetic_spans  # noqa: E402

__all__ = ["import_material", "load_yaml", "obs_setup", "plan", "synthetic_spans"]
