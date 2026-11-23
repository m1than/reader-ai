"""INV-INGEST-4 at run time: the cross-process probes under the product profile, at setup and watcher start.

The probe (`cross_process_probe.txt`, the [host] test module's bytes) runs confined in a fresh probe WORK
against a second confined sleeper. Any "other" channel not refused with EPERM, a probe that fails or prints
no JSON, makes the host state `setup_missing`: no conversion runs under a profile that leaks. Only probe
names are logged.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from typing import Final

from reader.ingest.host import sandbox

log = logging.getLogger("reader.ingest.host")

PROBE_SOURCE: Final = Path(__file__).resolve().with_name("cross_process_probe.txt")
REFUSED: Final = "denied:errno 1"
PROBE_TIMEOUT_S: Final = 60.0


def judge(code: int, raw: bytes) -> str:
    """`ready` when the probe exited 0 and every channel against the other process was refused with EPERM."""
    if code != 0:
        log.warning("cross_process_probe_failed exit=%d", code)
        return "setup_missing"
    try:
        other = json.loads(raw)["other"]
    except (ValueError, KeyError, TypeError):
        log.warning("cross_process_probe_failed reason=no_json")
        return "setup_missing"
    if not isinstance(other, dict) or not other:
        log.warning("cross_process_probe_failed reason=no_channels")
        return "setup_missing"
    leaking = sorted(str(name) for name, value in other.items() if value != REFUSED)
    if leaking:
        log.warning("cross_process_probe_leak channels=%s", ",".join(leaking))
        return "setup_missing"
    return "ready"


def run(venv: Path, pyroot: Path, models: Path, data_dir: Path) -> str:
    """Run the probes once under the product profile; returns `ready` or `setup_missing`."""
    root = Path(os.path.realpath(data_dir)) / "ingest" / f"probe-{secrets.token_hex(8)}"
    work_a, work_b = root / "a" / "w", root / "b" / "w"
    for work in (work_a, work_b):
        for sub in ("in", "home", "tmp"):
            (work / sub).mkdir(parents=True, mode=0o700)
    probe = work_a / "cross_process_probe.py"
    shutil.copyfile(PROBE_SOURCE, probe)

    def confined(work: Path, tail: list[str]) -> list[str]:
        argv = sandbox.child_argv(venv, pyroot, models, work)
        python_at = max(i for i, a in enumerate(argv) if a == "-D") + 2
        return [*argv[: python_at + 1], "-I", *tail]

    sleeper = subprocess.Popen(  # noqa: S603 (the product profile's argv)
        confined(work_b, ["-c", "import time; time.sleep(60)", "--input", f"{work_b}/in/material.pdf"]),
        env=sandbox.child_env(work_b, 1),
        cwd=work_b,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(1.5)
        cfg = {
            "target": sleeper.pid,
            "work_b": str(work_b),
            "work_b_dev": os.stat(work_b).st_dev,
            "work_b_ino": os.stat(work_b).st_ino,
        }
        try:
            done = subprocess.run(  # noqa: S603
                confined(work_a, [str(probe), json.dumps(cfg)]),
                env=sandbox.child_env(work_a, 1),
                cwd=work_a,
                capture_output=True,
                timeout=PROBE_TIMEOUT_S,
                check=False,
            )
            code, raw = done.returncode, done.stdout
        except subprocess.TimeoutExpired:
            code, raw = -1, b""
        alive = sleeper.poll() is None
    finally:
        sleeper.kill()
        sleeper.wait()
        shutil.rmtree(root, ignore_errors=True)
    if not alive:
        log.warning("cross_process_probe_failed reason=target_gone")
        return "setup_missing"
    return judge(code, raw)
