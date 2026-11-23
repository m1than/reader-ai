"""Spike ingest-sysctl: which sysctl names each profile refuses, read from inside the sandbox.

    uv run --project server python -I spikes/ingest-sysctl/sweep.py <data_dir> <scratch dir> <out.json> <profile.sb>...

The unconfined driver lists every name (`sysctl -aN`); a confined child calls `sysctlbyname` with a size query for
each and reports the names that fail with EPERM. Names only, no values.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from reader.ingest.host import sandbox, setup

CHILD = r"""
import ctypes, json, sys
libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
refused, other = [], {}
for name in json.load(open(sys.argv[1])):
    size = ctypes.c_size_t(0)
    if libc.sysctlbyname(name.encode(), None, ctypes.byref(size), None, 0) != 0:
        err = ctypes.get_errno()
        (refused.append(name) if err == 1 else other.setdefault(str(err), []).append(name))
print(json.dumps({"refused": refused, "other_errors": other}))
"""


def main() -> None:
    data, scratch, out = Path(sys.argv[1]), Path(os.path.realpath(sys.argv[2])), Path(sys.argv[3])
    names = sorted(set(subprocess.run(["/usr/sbin/sysctl", "-aN"], capture_output=True, text=True).stdout.split()))
    names += ["kern.proc.pid." + str(os.getpid()), "kern.procargs2." + str(os.getpid())]
    report: dict[str, object] = {"names": len(names)}
    venv = setup.venv_dir(data)
    for profile in sys.argv[4:]:
        work = scratch / Path(profile).stem
        shutil.rmtree(work, ignore_errors=True)
        for sub in ("in", "home", "tmp"):
            (work / sub).mkdir(parents=True)
        (work / "names.json").write_text(json.dumps(names))
        (work / "child.py").write_text(CHILD)
        argv = sandbox.child_argv(venv, setup.pyroot_of(venv), setup.models_dir(data), work)
        argv[2] = str(Path(profile).resolve())
        at = max(i for i, a in enumerate(argv) if a == "-D") + 2
        done = subprocess.run(
            [*argv[: at + 1], "-I", str(work / "child.py"), str(work / "names.json")],
            env=sandbox.child_env(work, 1), cwd=work, capture_output=True, text=True, timeout=300,
        )
        report[Path(profile).name] = json.loads(done.stdout) if done.returncode == 0 else {"error": done.stderr[-300:]}
        shutil.rmtree(work, ignore_errors=True)
    out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
