"""SRV-OBS [unit]: lab verification's `kernel.run` child span (FR-OBS-13) — one per run of the labs, under the
current span, with the lab count and the exit flag; no code or output in it (INV-OBS-2).

@trace FR-OBS-13 INV-OBS-2
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from obs.conftest import finished, obs_setup, require

from .conftest import need
from .test_verify import FakeKernel, _lab_nb, _Ws

pytestmark = pytest.mark.anyio
CODE_CANARY = "codecanary-3e1f"
OUTPUT_CANARY = "outputcanary-8a2d"


class _FailingAsserts(_Ws):
    """Every assert answers an error, so each lab fails."""

    async def send(self, frame: str) -> None:
        await super().send(frame)
        if json.loads(frame)["content"]["code"].startswith("assert"):
            for message in self.queue:
                if message["header"]["msg_type"] == "execute_reply":
                    message["content"]["status"] = "error"


class FailingKernel(FakeKernel):
    async def ws(self, endpoint: Any, kernel: str) -> Any:
        return _FailingAsserts(self)


async def _spans(kernel: FakeKernel, labs: int) -> tuple[list[Any], Any]:
    verify = need("reader.pipeline.verify")
    nb = _lab_nb(["True"], source=f"print('{CODE_CANARY}')", n=labs)
    with obs_setup("worker") as (handle, memory):
        with require("reader.obs").tracer().start_as_current_span("job pipeline.verify") as root:
            await verify.VerifyClient(http=kernel.http, ws=kernel.ws).run(nb)
        return finished(handle, memory), root


@pytest.mark.parametrize(("kernel", "exit_flag"), [
    (FakeKernel([f"{OUTPUT_CANARY}\n", f"{OUTPUT_CANARY}\n"]), 0),
    (FailingKernel([f"{OUTPUT_CANARY}\n", f"{OUTPUT_CANARY}\n"]), 1),
])
async def test_one_kernel_run_span_per_run(kernel: FakeKernel, exit_flag: int) -> None:
    spans, root = await _spans(kernel, labs=2)
    runs = [s for s in spans if s.name == "kernel.run"]
    assert len(runs) == 2  # two runs of the labs (determinism check)
    for run in runs:
        assert run.parent is not None and run.parent.span_id == root.get_span_context().span_id
        assert run.attributes["reader.kernel.cells"] == 2
        assert run.attributes["reader.kernel.exit"] == exit_flag
        assert run.status.status_code.name == "OK"
    dumped = json.dumps([dict(s.attributes) for s in spans])
    assert CODE_CANARY not in dumped and OUTPUT_CANARY not in dumped
