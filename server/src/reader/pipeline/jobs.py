"""SRV-PIPELINE's job kinds (SRV-EVENTS KIND_MODULES): one step of a compile run per job.

`pipeline.stage` runs model steps and the findings step (timeout `stage_job_timeout_s`);
`pipeline.verify` runs lab verification (`verify_job_timeout_s`). Both are keyed by
(run_id, step, stage, input_sha256).
"""

from __future__ import annotations

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

JOB_KINDS: Final = (
    JobKind(
        KIND_STAGE, "pipeline", StageArgs, KEY, handle, STAGE_MAX_ATTEMPTS, TRANSIENT, STAGE_JOB_TIMEOUT_S
    ),
    JobKind(
        KIND_VERIFY, "pipeline", StageArgs, KEY, handle, STAGE_MAX_ATTEMPTS, TRANSIENT, VERIFY_JOB_TIMEOUT_S
    ),
)


async def on_worker_start(jobs: Any) -> None:
    """FR-PIPELINE-6: ensure every unfinished run at worker start."""
    await ensure_all(jobs.engine, jobs)


__all__ = ["JOB_KINDS", "Runtime", "StageArgs", "configure", "on_worker_start"]
