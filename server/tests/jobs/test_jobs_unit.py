"""SRV-EVENTS [unit]: kinds, keys, lock ids, payload checks, the state machine, the status map, the logs.

@trace FR-EVENTS-1 FR-EVENTS-2 FR-EVENTS-10 FR-EVENTS-12 FR-EVENTS-13 FR-EVENTS-14 FR-EVENTS-16
@trace INV-EVENTS-5 INV-EVENTS-6 VEC-EVENTS-1 VEC-EVENTS-2 VEC-EVENTS-3
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from .conftest import FIXTURES, KIND, effect_kind, load_yaml, require_jobs

TRANSITIONS = load_yaml(FIXTURES / "transitions.yaml")["rows"]
STATUS_MAP = load_yaml(FIXTURES / "status-map.yaml")["rows"]
KEY_CASES = load_yaml(FIXTURES / "keys.yaml")["cases"]
PROCRASTINATE_STATUSES = ["todo", "doing", "succeeded", "failed", "cancelled", "aborting", "aborted"]
MODEL_EVENTS = [
    "enqueue", "claim", "ok", "fail_transient", "fail_permanent", "stall", "stall_live", "abort", "requeue"
]


def _kind_named(kind_name: str, **fields: Any) -> Any:
    from pydantic import BaseModel, ConfigDict, create_model

    jobs = require_jobs()
    config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    model = create_model("A", __config__=config, **fields) if fields else None
    if model is None:

        class A(BaseModel):
            model_config = ConfigDict(extra="forbid")
            name: str

        model = A

    async def handler(*_: Any) -> None:
        return None

    return jobs.JobKind(name=kind_name, queue="q", args=model, key=("name",), handler=handler)


# ---- FR-EVENTS-1: the catalog


def test_catalog_accepts_declared_kinds_and_lists_queues() -> None:
    jobs = require_jobs()
    catalog = jobs.Catalog([effect_kind()])
    assert list(catalog) == [KIND]
    assert catalog.queues == ["test"]


@pytest.mark.parametrize(
    ("names", "code"),
    [
        (["jobs.sweep"], "kind_name_reserved"),
        (["jobs.judge_reap"], "kind_name_reserved"),  # FR-EVENTS-1 (e), ADR-0039 B7b
        (["jobs.judge_gc"], "kind_name_reserved"),
        (["obs.health.tick"], "kind_name_reserved"),
        (["Bad"], "kind_name_invalid"),
        (["ab"], "kind_name_invalid"),
        (["dup.kind", "dup.kind"], "kind_name_duplicate"),
    ],
)
def test_catalog_rejects_bad_names(names: list[str], code: str) -> None:
    jobs = require_jobs()
    with pytest.raises(jobs.JobError) as caught:
        jobs.Catalog([_kind_named(n) for n in names])
    assert caught.value.code == code


def test_catalog_rejects_missing_key_field_and_secret_fields() -> None:
    import dataclasses

    from reader.config import Secret

    jobs = require_jobs()
    kind = _kind_named("ok.kind")
    with pytest.raises(jobs.JobError) as missing:
        jobs.Catalog([dataclasses.replace(kind, key=("absent",))])
    assert missing.value.code == "kind_key_field_missing"
    secretive = _kind_named("sec.kind", name=(str, ...), token=(Secret, ...))
    with pytest.raises(jobs.JobError) as secret:
        jobs.Catalog([secretive])
    assert secret.value.code == "secret_in_payload"
    nested = _kind_named("nest.kind", name=(str, ...), extra=(list[Secret] | None, None))
    with pytest.raises(jobs.JobError) as deep:
        jobs.Catalog([nested])
    assert deep.value.code == "secret_in_payload"


@pytest.mark.parametrize(
    ("priority", "ok"), [(0, True), (100, True), (999_999, True), (-1, False), (1_000_000, False)]
)
def test_catalog_bounds_priority_below_the_sweep(priority: int, ok: bool) -> None:
    import dataclasses

    from reader.jobs.queue import SWEEP_PRIORITY

    jobs = require_jobs()
    assert SWEEP_PRIORITY == 1_000_000
    kind = _kind_named("pri.kind")
    assert kind.priority == 0  # the default
    if ok:
        assert jobs.Catalog([dataclasses.replace(kind, priority=priority)])["pri.kind"].priority == priority
        return
    with pytest.raises(jobs.JobError) as caught:
        jobs.Catalog([dataclasses.replace(kind, priority=priority)])
    assert caught.value.code == "kind_bounds_invalid"


@pytest.mark.parametrize(
    ("seconds", "ok"), [(None, True), (1, True), (86_400, True), (0, False), (-5, False)]
)
def test_catalog_bounds_key_retention(seconds: int | None, ok: bool) -> None:
    """FR-EVENTS-1 (d): `key_retention_s` is positive or None."""
    import dataclasses

    jobs = require_jobs()
    kind = dataclasses.replace(_kind_named("ret.kind"), key_retention_s=seconds)
    if ok:
        assert jobs.Catalog([kind])["ret.kind"].key_retention_s == seconds
        return
    with pytest.raises(jobs.JobError) as caught:
        jobs.Catalog([kind])
    assert caught.value.code == "kind_bounds_invalid"


def test_kind_modules_are_a_declared_list() -> None:
    jobs = require_jobs()
    assert isinstance(jobs.KIND_MODULES, tuple)
    assert len(jobs.load_kinds(())) == 0
    # SRV-INGEST, SRV-SEARCH, SRV-PIPELINE, SRV-TUTOR, SRV-OBS, SRV-FRESH paired changes (FR-EVENTS-1, -19)
    assert jobs.KIND_MODULES == (
        "reader.ingest.jobs",
        "reader.search.jobs",
        "reader.pipeline.jobs",
        "reader.tutor.jobs",
        "reader.obs.jobs",
        "reader.freshness.jobs",
        "reader.agent.lane",  # FR-EVENTS-9 amended (ADR-0039 B7b)
    )
    assert list(jobs.load_kinds()) == [
        "ingest.import", "search.embed", "pipeline.stage", "pipeline.verify", "tutor.answer",
        "obs.feedback", "obs.prompts", "obs.retention",  # SRV-OBS FR-OBS-14, -15 (V.obs-analytics)
        "obs.setup", "obs.runs", "obs.bench", "obs.label", "obs.health",  # SRV-OBS phase 2 (FR-EVENTS-32)
        # SRV-OBS ADR-0039 (FR-EVENTS-32's paired note, V.kl-obs-spans)
        "obs.eval", "obs.eval_run", "obs.judge_score", "obs.eval_sample", "obs.align",
        "freshness.scan", "freshness.web",
        "judge.find",
    ]
    assert jobs.load_kinds()["judge.find"].key_retention_s == 86_400


def test_start_hooks_are_the_declared_modules_on_worker_start() -> None:
    """FR-EVENTS-1: `hooks=None` means these, in KIND_MODULES order (ingest and tutor export none)."""
    require_jobs()
    import reader.agent.lane
    import reader.obs.jobs
    import reader.pipeline.jobs
    import reader.search.jobs
    from reader.jobs.catalog import start_hooks

    assert start_hooks() == [
        reader.search.jobs.on_worker_start,
        reader.pipeline.jobs.on_worker_start,
        reader.obs.jobs.on_worker_start,
        reader.agent.lane.on_worker_start,  # the worker revokes its own stale run tokens (FR-AGENT-3)
    ]
    assert start_hooks(()) == []


# ---- FR-EVENTS-2, -13: keys and lock ids


@pytest.mark.parametrize("case", KEY_CASES, ids=lambda c: c["key"][-8:])
def test_idempotency_keys_match_vectors(case: dict[str, Any]) -> None:
    from pydantic import BaseModel

    jobs = require_jobs()
    fields = case["fields"]
    model = type("M", (BaseModel,), {"__annotations__": dict.fromkeys(fields, str)})

    async def handler(*_: Any) -> None:
        return None

    kind = jobs.JobKind(name=case["kind"], queue="q", args=model, key=tuple(fields), handler=handler)
    assert jobs.idempotency_key(kind, fields) == case["key"]


@given(st.text())
def test_lock_id_is_a_signed_int64_and_deterministic(key: str) -> None:
    jobs = require_jobs()
    assert -(2**63) <= jobs.lock_id(key) < 2**63
    assert jobs.lock_id(key) == jobs.lock_id(key)


def test_lock_id_uses_the_jobs_namespace() -> None:
    import hashlib

    import reader.db as db

    jobs = require_jobs()
    key = "test.effect:" + "0" * 64
    digest = hashlib.sha256(b"reader.jobs\0" + key.encode()).digest()
    assert jobs.lock_id(key) == int.from_bytes(digest[:8], "big", signed=True)
    assert db.MIGRATION_LOCK_NAMESPACE != 0  # SRV-AUTH uses the two-int4 form, a separate key space


# ---- FR-EVENTS-10: payload checks


def test_payload_check_rejects_secret_instances_and_values_in_leaves_and_keys() -> None:
    from reader.config import Secret

    require_jobs()
    from reader.jobs.model import JobError, check_payload

    canary = "CANARY-jobs-" + "z" * 10
    with pytest.raises(JobError) as instance:
        check_payload({"name": Secret(canary)}, {}, ())
    assert instance.value.code == "secret_in_payload"
    assert canary not in str(instance.value)
    for payload in ({"name": f"x{canary}y"}, {"nested": [{"deep": canary}]}, {"map": {canary: "v"}}):
        with pytest.raises(JobError) as leaf:
            check_payload(payload, payload, (canary,))
        assert leaf.value.code == "secret_in_payload"
        assert canary not in str(leaf.value)
    check_payload({"name": "clean"}, {"name": "clean"}, (canary,))


# ---- INV-EVENTS-6 / VEC-EVENTS-1: the state machine


def test_transitions_are_total_and_match_the_fixture() -> None:
    require_jobs()
    from reader.jobs.states import EVENTS, STATES, transition

    assert len(TRANSITIONS) == len(STATES) * len(EVENTS)
    seen = {(r["state"], r["event"]) for r in TRANSITIONS}
    assert seen == {(s, e) for s in STATES for e in EVENTS}
    for row in TRANSITIONS:
        step = transition(row["state"], row["event"])
        expected = (row["next"], row["result"], row["effect"], row["attempts"])
        assert (step.next, step.result, step.effect, step.attempts) == expected, row
    assert all(row["result"] != "invalid" for row in TRANSITIONS)


class KeyMachine(RuleBasedStateMachine):
    """Random event sequences on the model: effects per outcome epoch and counted runs stay bounded."""

    MAX_ATTEMPTS = 3

    def __init__(self) -> None:
        super().__init__()
        from reader.jobs.states import transition

        self.transition = transition
        self.state = "none"
        self.attempts = 0
        self.effects_since_requeue = 0

    def _apply(self, event: str) -> None:
        if event in ("fail_transient", "stall") and self.attempts + 1 >= self.MAX_ATTEMPTS:
            event = "fail_last" if event == "fail_transient" else "stall_last"
        step = self.transition(self.state, event)
        if step.attempts == "reset":
            self.attempts = 0
            self.effects_since_requeue = 0
        else:
            self.attempts += int(step.attempts)
        self.effects_since_requeue += step.effect
        self.state = step.next

    @rule(event=st.sampled_from(MODEL_EVENTS))
    def event(self, event: str) -> None:
        self._apply(event)

    @invariant()
    def bounded(self) -> None:
        assert self.effects_since_requeue <= 1
        assert self.attempts <= self.MAX_ATTEMPTS
        if self.state == "succeeded":
            assert self.effects_since_requeue == 1


def test_state_machine_properties() -> None:
    require_jobs()
    machine = KeyMachine.TestCase
    machine.settings = settings(max_examples=200, stateful_step_count=30, deadline=None)
    machine().runTest()


# ---- FR-EVENTS-12 / VEC-EVENTS-3: the status map


def test_status_map_matches_the_fixture() -> None:
    require_jobs()
    from reader.jobs.states import RECONCILE_ENQUEUE, RECONCILE_NONE, RECONCILE_STALL_LIKE, status_of

    assert len(STATUS_MAP) == 25
    for row in STATUS_MAP:
        outcome = None if row["outcome"] == "none" else row["outcome"]
        key_row = row.get("key_row", "present") == "present"
        status, action = status_of(row["deciding"], outcome, key_row)
        assert status == row["job_status"], row
        expected = row["reconcile"]
        if expected.startswith("stall-like"):
            assert action == RECONCILE_STALL_LIKE, row
        elif expected.startswith("enqueue"):
            assert action == RECONCILE_ENQUEUE, row
        else:
            assert action == RECONCILE_NONE, row


@given(
    st.lists(
        st.tuples(st.integers(1, 10_000), st.sampled_from(PROCRASTINATE_STATUSES)), unique_by=lambda t: t[0]
    )
)
def test_deciding_job_precedence(statuses: list[tuple[int, str]]) -> None:
    require_jobs()
    from reader.jobs.states import deciding_status

    got = deciding_status(statuses)
    present = {s for _, s in statuses}
    if "doing" in present:
        assert got == "doing"
    elif "aborting" in present:
        assert got == "aborting"
    elif "todo" in present:
        assert got == "todo"
    elif not statuses:
        assert got == "absent"
    else:
        assert got == max(statuses)[1]


# ---- FR-EVENTS-16: the log filter


def _record(name: str, message: str, job: dict[str, Any]) -> logging.LogRecord:
    record = logging.LogRecord(name, logging.INFO, __file__, 1, message, None, None)
    record.job = job
    return record


def test_log_filter_scrubs_procrastinate_records_and_is_idempotent() -> None:
    require_jobs()
    from reader.jobs.logfilter import JobRecordScrubber

    marker = "PAYLOAD-MARKER-" + "q" * 6
    call = f"test.effect[7](payload={{'name': '{marker}'}})"
    kwargs = {"payload": {"name": marker}}
    job = {"id": 7, "task_name": "test.effect", "task_kwargs": kwargs, "call_string": call}
    record = _record("procrastinate.worker", f"Starting job {call}", job)
    scrubber = JobRecordScrubber()
    assert scrubber.filter(record) is True
    assert marker not in record.getMessage()
    assert "test.effect[7]" in record.getMessage()
    assert "task_kwargs" not in record.job
    assert "call_string" not in record.job
    first = (record.getMessage(), dict(record.job))
    assert scrubber.filter(record) is True
    assert (record.getMessage(), dict(record.job)) == first
    other = _record("reader.jobs", f"mine {marker}", {"task_kwargs": 1})
    scrubber.filter(other)
    assert other.job == {"task_kwargs": 1}


def test_log_filter_masks_call_strings_without_a_job_extra() -> None:
    require_jobs()
    from reader.jobs.logfilter import JobRecordScrubber, mask_call_strings

    marker = "PAYLOAD-MARKER-" + "s" * 6
    call = f"test.effect[42](payload={{'name': '{marker} (x)', 'q': \")\"}})"
    line = f"Waiting for job to finish: worker: {call} (started 1.234 s ago)"
    record = logging.LogRecord("procrastinate.worker", logging.INFO, __file__, 1, line, None, None)
    JobRecordScrubber().filter(record)
    assert record.getMessage() == "Waiting for job to finish: worker: test.effect[42] (started 1.234 s ago)"
    assert mask_call_strings(f"a {call} b {call}") == "a test.effect[42] b test.effect[42]"
    assert mask_call_strings("no call here (x)") == "no call here (x)"
    deferring = f"About to defer job test.effect[None](payload={{'name': '{marker}'}})"
    assert mask_call_strings(deferring) == "About to defer job test.effect[None]"


def test_catalog_rejects_args_without_extra_forbid_and_secret_subclasses() -> None:
    from pydantic import BaseModel, ConfigDict

    from reader.config import Secret

    jobs = require_jobs()

    class Loose(BaseModel):
        name: str

    async def handler(*_: Any) -> None:
        return None

    loose = jobs.JobKind(name="loose.kind", queue="q", args=Loose, key=("name",), handler=handler)
    with pytest.raises(jobs.JobError) as caught:
        jobs.Catalog([loose])
    assert caught.value.code == "kind_args_not_forbid"

    class Token(Secret):
        pass

    class WithToken(BaseModel):
        model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
        name: str
        token: Token | None = None

    subclass = jobs.JobKind(name="tok.kind", queue="q", args=WithToken, key=("name",), handler=handler)
    with pytest.raises(jobs.JobError) as secret:
        jobs.Catalog([subclass])
    assert secret.value.code == "secret_in_payload"


def test_install_log_filter_targets_root_handlers() -> None:
    require_jobs()
    from reader.jobs.logfilter import JobRecordScrubber, install_log_filter

    handler = logging.Handler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        install_log_filter()
        install_log_filter()
        assert sum(isinstance(f, JobRecordScrubber) for f in handler.filters) == 1
    finally:
        root.removeHandler(handler)
