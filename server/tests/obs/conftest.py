"""Helpers for the SRV-OBS tests (spec/modules/SRV-OBS.yaml).

@trace FR-OBS-1

The modules under test (`reader.obs.*`) are imported inside test bodies or in helpers those bodies call, so in the
Red phase each test fails on its own. `obs_setup` installs tracing with an in-memory exporter behind the real
AllowListExporter, so a test reads exactly what would leave the process.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from ruamel.yaml import YAML

from jobs.conftest import pg, pg_container  # noqa: F401 (the [db] fixtures: one Postgres per session, a db per test)

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "spec" / "fixtures" / "SRV-OBS"
ROUTE_TEMPLATES = (
    "/api/lessons/{lesson_id}",
    "/api/healthz",
    "/api/obs/gate",
    "/api/kernel/sessions/{session_id}/channels",
)


def load_yaml(path: Path) -> Any:
    return YAML(typ="safe").load(path.read_text(encoding="utf-8"))


def require(module: str) -> Any:
    assert importlib.util.find_spec(module) is not None, f"{module} does not exist yet"
    return importlib.import_module(module)


def config(
    enabled: bool = True, retention_days: int = 30, tasks: tuple[str, ...] = ("draft", "tutor")
) -> Any:
    """A config-shaped object: setup reads `observability` and the task names only."""
    return SimpleNamespace(
        observability=SimpleNamespace(enabled=enabled, retention_days=retention_days),
        tasks={t: SimpleNamespace(model="sonnet") for t in tasks},
    )


def catalogs(**overrides: Any) -> Any:
    attrs = require("reader.obs.attributes")
    values = {
        "route_templates": frozenset(ROUTE_TEMPLATES),
        "job_kinds": frozenset(
            {
                "pipeline.stage",
                "pipeline.verify",
                "tutor.answer",
                "ingest.import",
                "search.embed",
                "obs.retention",
                "freshness.scan",
                "freshness.web",
                "test.effect",
            }
        ),
        "pipeline_stages": frozenset({"draft", "fact_check", "adversarial_review", "revise", "verify"}),
        # fact_check: tests/agent's configured task; the freshness tasks: SRV-FRESH's (paired change)
        "tasks": frozenset(
            {"draft", "fact_check", "tutor", "freshness_scan", "freshness_web", "freshness_report"}
        ),
        "models": frozenset({"claude-sonnet-5-5", "claude-opus-5-5"}),
    }
    values.update(overrides)
    return attrs.Catalogs(**values)


@contextmanager
def kept_state() -> Iterator[None]:
    """`reader.obs._STATE` (the provider, the version key) as it was before, whatever the body set (code review of
    8466329: no test leaks a version key or a provider into the next)."""
    import reader.obs

    before = dict(reader.obs._STATE)
    try:
        yield
    finally:
        reader.obs._STATE.clear()
        reader.obs._STATE.update(before)


@pytest.fixture(autouse=True)
def _reset_obs_state() -> Iterator[None]:
    with kept_state():
        yield


@contextmanager
def obs_setup(role: str = "api", enabled: bool = True, **cat: Any) -> Iterator[Any]:
    """Tracing installed with an InMemorySpanExporter behind the AllowListExporter; yields (handle, exported)."""
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    setup = require("reader.obs.setup")
    memory = InMemorySpanExporter()
    with kept_state():
        handle = setup.setup(role, config(enabled), exporter=memory, catalogs=catalogs(**cat))
        try:
            yield handle, memory
        finally:
            handle.shutdown()


def finished(handle: Any, memory: Any) -> list[Any]:
    handle.force_flush()
    return list(memory.get_finished_spans())
