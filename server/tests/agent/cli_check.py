"""Run inside reader-server:local by test_cli_image.py (not collected: no `test_` prefix).

Starts the handshake spike's stub on 127.0.0.1:8080, then drives the real bundled CLI through
`AgentProvider._drive` (the adapter's CLI path: system prompt file, environment, kill on deadline) and prints
one JSON line. No network, no token, no credit.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from decimal import Decimal
from pathlib import Path

from reader.agent.errors import AgentError
from reader.agent.provider import AgentProvider, StageCall, _Seen


def _cli_processes() -> int:
    count = 0
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                if b"_bundled" in (entry / "cmdline").read_bytes():
                    count += 1
            except OSError:
                continue
    return count


async def main(mode: str) -> dict[str, object]:
    provider = AgentProvider(None, None, None, proxy_url="http://127.0.0.1:8080")  # type: ignore[arg-type]
    home = Path(tempfile.mkdtemp(dir="/tmp"))
    system = "x" * 300_000 if mode == "big" else "Answer with one word."
    deadline = 2.0 if mode == "deadline" else None
    call = StageCall(
        owner_id=uuid.uuid4(),
        handle="h",
        run_id=uuid.uuid4(),
        lesson_id=None,
        stage="check",
        task="t",
        attempt=0,
        system=system,
        prompt="Reply with the word ok.",
        max_output_tokens=1000,
        floor_usd=Decimal(0),
        run_cap_usd=Decimal(1),
        deadline_s=deadline,
    )
    seen = _Seen()
    started = time.monotonic()
    out: dict[str, object] = {}
    try:
        await provider.drive_bounded(
            call, "claude-sonnet-5-5", Decimal("0.5"), os.environ["HANDLE"], home, seen
        )
        out["ok"] = seen.result is not None and not seen.result.is_error
        out["init_tools"] = (seen.init or {}).get("tools")
    except AgentError as error:
        out["timeout"] = error.code == "deadline_exceeded"
    out["elapsed_s"] = round(time.monotonic() - started, 2)
    await asyncio.sleep(0.3)
    out["cli_processes_left"] = _cli_processes()
    return out


if __name__ == "__main__":
    stub = subprocess.Popen(
        [sys.executable, "/stub/stub.py"], stdin=subprocess.PIPE, stdout=subprocess.PIPE
    )
    assert stub.stdout is not None
    stub.stdout.readline()  # stub-ready
    sys.stdout.write(json.dumps(asyncio.run(main(sys.argv[1]))) + "\n")
