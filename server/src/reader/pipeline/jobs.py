"""SRV-PIPELINE's job kinds (SRV-EVENTS KIND_MODULES): one step of a compile run per job.

`pipeline.stage` runs model steps and the findings step (timeout `stage_job_timeout_s`);
`pipeline.verify` runs lab verification (`verify_job_timeout_s`). Both are keyed by
(run_id, step, stage, input_sha256).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from reader.agent.errors import TransientAgentError
from reader.jobs import JobKind
from reader.pipeline.errors import TransientPipelineError
from reader.pipeline.runs import KIND_STAGE, KIND_VERIFY, ensure_all
from reader.pipeline.runtime import Runtime, configure
from reader.pipeline.steps import handle

STAGE_JOB_TIMEOUT_S: Final = 1200.0  # bounds.stage_job_timeout_s
VERIFY_JOB_TIMEOUT_S: Final = 3600.0  # bounds.verify_job_timeout_s
STAGE_MAX_ATTEMPTS: Final = 3  # bounds.stage_max_attempts
TRANSIENT: Final = (TransientAgentError, TransientPipelineError)


class StageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    step: int
    stage: str
    input_sha256: str


KEY: Final = ("run_id", "step", "stage", "input_sha256")

# FR-PIPELINE-18: a run's end is counted only by the step that ended it: a succeeded step of a finished, not
# cancelled run whose step is at least every recorded step (the final step, whatever its stage: lab
# verification or a budget refusal), or a dead step of a run that is not finished (it stops failed). A cancel
# counts its own end (runs.cancel). Counted once per committed end.
_ENDED_SQL: Final = (
    "UPDATE compile_runs r SET ends = ends + 1 WHERE r.id = CAST(:r AS uuid) AND r.ends < 100"
    " AND r.cancelled_at IS NULL"
    " AND CASE WHEN :dead THEN r.finished_at IS NULL ELSE r.finished_at IS NOT NULL"
    " AND :step >= coalesce((SELECT max(s.step) FROM compile_steps s WHERE s.run_id = r.id), 0) END"
    " RETURNING r.ends"
)


async def after_end(engine: Any, payload: Mapping[str, Any], outcome: str) -> None:
    from sqlalchemy import text

    from reader.obs import runs as obs_runs

    async with engine.begin() as conn:  # the end is counted with tracing off too; only the enqueue is skipped
        ends = (await conn.execute(text(_ENDED_SQL), {
            "r": str(payload["run_id"]), "dead": outcome == "dead", "step": int(payload["step"])})).scalar()
    if ends is not None:
        await obs_runs.enqueue_end("compile", str(payload["run_id"]), int(ends))


JOB_KINDS: Final = (
    JobKind(
        KIND_STAGE, "pipeline", StageArgs, KEY, handle, STAGE_MAX_ATTEMPTS, TRANSIENT, STAGE_JOB_TIMEOUT_S,
        after_end=after_end,
    ),
    JobKind(
        KIND_VERIFY, "pipeline", StageArgs, KEY, handle, STAGE_MAX_ATTEMPTS, TRANSIENT, VERIFY_JOB_TIMEOUT_S,
        after_end=after_end,
    ),
)


async def on_worker_start(jobs: Any) -> None:
    """FR-PIPELINE-6: ensure every unfinished run at worker start."""
    await ensure_all(jobs.engine, jobs)


__all__ = ["JOB_KINDS", "Runtime", "StageArgs", "configure", "on_worker_start"]
