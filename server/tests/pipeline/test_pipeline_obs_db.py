"""SRV-OBS [db]: the compile loop's call sites of the obs helpers on the real job machinery — the queue's session
marker on every pipeline job, the root keys a step sets (lesson, course, prompt version), the fact check's
`verify.claims` and the lab step's `verify.lab` child spans with their counts, and the code assessments
enqueued as `obs.feedback` (exact args), with no source text or model output in any span or job arg.

@trace FR-OBS-12 FR-OBS-13 FR-OBS-14 FR-OBS-18 INV-OBS-2 INV-OBS-3
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from obs.conftest import finished, obs_setup

from .conftest import CANARY, FIXTURES, Pg, lab_modes, load_yaml, pipeline_env, scripts_for

pytestmark = pytest.mark.anyio
CASES = {c["name"]: c for c in load_yaml(FIXTURES / "loop-cases.yaml")["cases"]}
RESERVES = load_yaml(FIXTURES / "loop-cases.yaml")["defaults"]["reserves"]
PROMPT_VERSION = 7


class Captured:
    """reader.jobs.running(): records obs.feedback enqueues (the pipeline's own enqueues use rt.jobs())."""

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict[str, Any], int]] = []

    async def enqueue(self, kind: str, args: Any, schedule_in: int = 0) -> Any:
        self.enqueued.append((kind, dict(args), schedule_in))
        return None


def _patch(monkeypatch: pytest.MonkeyPatch) -> Captured:
    import reader.jobs
    from reader.obs import prompts

    captured = Captured()
    monkeypatch.setattr(reader.jobs, "running", lambda: captured)

    async def version_of(name: str) -> int | None:
        return PROMPT_VERSION

    monkeypatch.setattr(prompts, "version_of", version_of)
    return captured


async def _run(pg: Pg, tmp: Path, case: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Any:
    captured = _patch(monkeypatch)
    with obs_setup("worker") as (handle, memory):
        async with pipeline_env(pg, tmp, labs=case.get("labs", "pass"), floors=RESERVES) as env:
            env.cli.scripts[:] = scripts_for(case["results"], case.get("labs", "pass"))
            env.verify.modes[:] = lab_modes(case["results"])
            await env.import_plan()
            run = await env.compile()
            for _ in range(6):
                await env.work()
            course = await env.scalar("SELECT course_id FROM lessons WHERE id = :l", l=env.lesson)
            spans = finished(handle, memory)
            # Phase 2 (FR-PIPELINE-18): the run's end also enqueues `obs.runs`, pinned by its own test.
            feedback = [e for e in captured.enqueued if e[0] != "obs.runs"]
            return run, env.lesson, course, spans, feedback


def _children(spans: list[Any], name: str) -> list[tuple[Any, Any]]:
    """(child, its parent root) pairs for every exported span called `name`."""
    by_id = {s.context.span_id: s for s in spans}
    return [(s, by_id[s.parent.span_id]) for s in spans if s.name == name]


def _no_canary(spans: list[Any], enqueued: list[Any]) -> None:
    dumped = json.dumps([dict(s.attributes) for s in spans], default=str) + json.dumps(enqueued)
    for canary in (CANARY, "Claim 1.", "Paragraph 0", "## Consensus", "x = 21 * 2"):
        assert canary not in dumped  # INV-OBS-2: no source text, notebook text or model output


async def test_loop_spans_root_keys_and_assessments(pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ids = pytest.importorskip("reader.obs.ids")
    run, lesson, course, spans, enqueued = await _run(pg, tmp_path, CASES["one-loop-converges"], monkeypatch)
    roots = [s for s in spans if s.name in ("job pipeline.stage", "job pipeline.verify")]
    # SRV-PIPELINE's ADR-0039 order (paired note in this spec's header): draft, rules, lab, fact_check, revise,
    # rules, lab, fact_check, review, findings; the obs call sites fire once per check run
    assert len(roots) == 10
    assert [s.name for s in roots].count("job pipeline.verify") == 2
    # FR-OBS-12 (e): the queue's marker under every pipeline root, carrying the run as the session.
    markers = _children(spans, "job.session")
    assert sorted(p.context.span_id for _, p in markers) == sorted(r.context.span_id for r in roots)
    assert {m.attributes["session.id"] for m, _ in markers} == {str(run)}
    assert {m.attributes["reader.run_id"] for m, _ in markers} == {str(run)}
    # FR-OBS-5 (e), FR-OBS-18: every step's root carries the lesson, the course and the prompt version.
    for root in roots:
        assert root.attributes["reader.lesson.id"] == str(lesson)
        assert root.attributes["reader.course.id"] == str(course)
        assert root.attributes["reader.prompt.version"] == PROMPT_VERSION
    # FR-OBS-12 (d), SRV-AGENT FR-AGENT-14 (code review round 2): every step's `agent.call`, from the provider,
    # carries the lesson, the course and the step its root stage job ran.
    agent_calls = _children(spans, "agent.call")
    assert agent_calls, "no agent.call span"
    for call, parent in agent_calls:
        assert call.attributes["reader.lesson.id"] == str(lesson)
        assert call.attributes["reader.course.id"] == str(course)
        assert call.attributes["reader.step"] == parent.attributes["reader.step"]
    assert len({c.attributes["reader.step"] for c, _ in agent_calls}) > 1  # the steps differ, not one constant
    # FR-OBS-13: the two fact checks' claim counts (6 claims; 5 then 6 supported), OK status, under their roots.
    claims = _children(spans, "verify.claims")
    assert sorted((c.attributes["reader.claims.total"], c.attributes["reader.claims.supported"]) for c, _ in claims) \
        == [(6, 5), (6, 6)]
    assert all(c.status.status_code.name == "OK" for c, _ in claims)
    assert {p.attributes["reader.stage"] for _, p in claims} == {"fact_check"}
    labs = _children(spans, "verify.lab")
    assert len(labs) == 2
    for lab, _ in labs:
        assert (lab.attributes["reader.kernel.cells"], lab.attributes["reader.kernel.cells_ok"]) == (1, 1)
        assert isinstance(lab.attributes["reader.kernel.ms"], int) and lab.attributes["reader.kernel.ms"] >= 0
        assert lab.status.status_code.name == "OK"
    # FR-OBS-14: the exact obs.feedback args, each pointing at the trace of the job that computed it.
    assert all(kind == "obs.feedback" and delay == 60 for kind, _, delay in enqueued)
    got = sorted((a["name"], a["value"], json.dumps(a["metadata"], sort_keys=True)) for _, a, _ in enqueued)
    assert got == sorted([
        ("fact_check.passed", False, json.dumps({"claims": 6, "supported": 5})),
        ("fact_check.passed", True, json.dumps({"claims": 6, "supported": 6})),
        *[("verify.lab_passed", True, json.dumps({"cells": 1, "cells_ok": 1}))] * 2,
        *[("verify.passed", True, "{}")] * 2,
    ])
    traces: dict[tuple[str, bool], set[Any]] = {}
    for _, a, _ in enqueued:
        traces.setdefault((a["name"], a["value"]), set()).add(ids.derive_trace_id(a["job_id"], a["key"]))
    fact_traces = {p.context.trace_id for _, p in claims}
    assert traces[("fact_check.passed", False)] | traces[("fact_check.passed", True)] == fact_traces
    lab_traces = {root.context.trace_id for _, root in labs}
    assert traces[("verify.lab_passed", True)] == lab_traces
    assert traces[("verify.passed", True)] == lab_traces
    assert all(a["key"].startswith("pipeline.") for _, a, _ in enqueued)
    _no_canary(spans, enqueued)


async def test_failed_labs_assess_false(pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # SRV-PIPELINE's ADR-0039 order: the draft's lab fails (a loop), the revised notebook's lab passes
    _, _, _, spans, enqueued = await _run(pg, tmp_path, CASES["lab-fail-loops-back"], monkeypatch)
    labs = sorted(
        (lab.attributes["reader.kernel.cells"], lab.attributes["reader.kernel.cells_ok"])
        for lab, _ in _children(spans, "verify.lab")
    )
    assert labs == [(1, 0), (1, 1)]
    got = sorted((a["name"], a["value"], json.dumps(a["metadata"], sort_keys=True)) for _, a, _ in enqueued)
    assert got == sorted([
        ("verify.lab_passed", False, json.dumps({"cells": 1, "cells_ok": 0})),
        ("verify.lab_passed", True, json.dumps({"cells": 1, "cells_ok": 1})),
        ("verify.passed", False, "{}"),
        ("verify.passed", True, "{}"),
        ("fact_check.passed", True, json.dumps({"claims": 6, "supported": 6})),
    ])
    _no_canary(spans, enqueued)
