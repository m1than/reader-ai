"""Spike ingest-sysctl driver: the round-3 attack under the product profile and under the spike profile.

    uv run --project server python -I spikes/ingest-sysctl/run.py <data_dir> <scratch dir> <out.json>

For each profile: run B is a confined `python -c sleep` whose argv names WORK_B and whose cwd is WORK_B (the
converter's argv shape, FR-INGEST-4); run A is spikes/ingest-sysctl/probe.py confined to WORK_A. The driver then
applies SRV-INGEST FR-INGEST-22's membership rule for run B to the attacker process probe.py forked. Prints and
writes numbers and verdicts only; no book text is involved.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

from reader.ingest.host import sandbox, setup

HERE = Path(__file__).resolve().parent
PROFILES = {"product": sandbox.PROFILE, "deny-by-name": HERE / "converter.sb", "v2": HERE / "converter-v2.sb",
            "v3": HERE / "converter-v3.sb", "v4": HERE / "converter-v4.sb", "v5": HERE / "converter-v5.sb"}


def confined(profile: Path, work: Path, data: Path, tail: list[str]) -> list[str]:
    venv = setup.venv_dir(data)
    argv = sandbox.child_argv(venv, setup.pyroot_of(venv), setup.models_dir(data), work)
    argv[2] = str(profile)
    argv[3:3] = ["-D", f"WORKROOT={os.path.realpath(work.parent)}"]  # used by v5 only; unused params are ignored
    python_at = max(i for i, a in enumerate(argv) if a == "-D") + 2
    return [*argv[: python_at + 1], "-I", *tail]


def member_of(pid: int, work: str) -> dict[str, object]:
    """FR-INGEST-22's argv and cwd arms for one candidate, as the unconfined watcher would see it."""
    args = subprocess.run(["/bin/ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True).stdout
    cwd = subprocess.run(
        ["/usr/sbin/lsof", "-a", "-d", "cwd", "-p", str(pid), "-Fn"], capture_output=True, text=True
    ).stdout
    cwd_path = next((line[1:] for line in cwd.splitlines() if line.startswith("n")), "")
    by_argv = (work + "/") in args
    by_cwd = cwd_path == work or cwd_path.startswith(work + "/")
    return {"alive": bool(args.strip()), "by_argv": by_argv, "by_cwd": by_cwd, "member": by_argv or by_cwd}


def one(name: str, profile: Path, data: Path, scratch: Path) -> dict[str, object]:
    root = Path(os.path.realpath(scratch)) / name
    shutil.rmtree(root, ignore_errors=True)
    work_a, work_b = root / secrets.token_hex(8), root / secrets.token_hex(8)
    for w in (work_a, work_b):
        (w / "in").mkdir(parents=True)
        (w / "home").mkdir()
        (w / "tmp").mkdir()
    shutil.copyfile(HERE / "probe.py", work_a / "probe.py")
    env_b = sandbox.child_env(work_b, 1)
    run_b = subprocess.Popen(
        confined(profile, work_b, data, ["-c", "import time; time.sleep(30)", "--input", f"{work_b}/in/material.pdf"]),
        env=env_b,
        cwd=work_b,
        start_new_session=True,
    )
    time.sleep(1.5)
    done = subprocess.run(
        confined(
            profile,
            work_a,
            data,
            [
                str(work_a / "probe.py"),
                json.dumps(
                    {
                        "target": run_b.pid,
                        "work_b": str(work_b),
                        "work_b_dev": os.stat(work_b).st_dev,
                        "work_b_ino": os.stat(work_b).st_ino,
                    }
                ),
            ],
        ),
        env=sandbox.child_env(work_a, 1),
        cwd=work_a,
        capture_output=True,
        text=True,
        timeout=60,
    )
    result: dict[str, object] = {"probe_exit": done.returncode, "stderr_bytes": len(done.stderr)}
    try:
        result.update(json.loads(done.stdout.strip().splitlines()[-1]))
    except (ValueError, IndexError):
        result["probe_output"] = "unparseable"
        result["probe_stderr_tail"] = done.stderr[-400:]  # the probe's own traceback; no material is involved
    attacker = result.get("attacker_pid")
    if isinstance(attacker, int):
        result["attacker_vs_run_b"] = member_of(attacker, str(work_b))
        try:
            os.kill(attacker, 9)
        except ProcessLookupError:
            pass
    run_b.kill()
    run_b.wait()
    shutil.rmtree(root, ignore_errors=True)
    return result


def main() -> None:
    data, scratch, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    report = {name: one(name, profile, data, scratch) for name, profile in PROFILES.items()}
    out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
