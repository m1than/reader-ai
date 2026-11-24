"""NFR-INGEST-1 gate under a spike profile: one full conversion of the pilot, numbers only.

    uv run --project server python -I spikes/ingest-sysctl/fullrun.py <profile.sb> <pilot.pdf> <data_dir> <work_dir> <out.json>

Points `reader.ingest.host.sandbox.PROFILE` at the given profile for this process, converts the whole PDF with the
stack's own confined converter (as spikes/tutor-chunks does), samples the resident memory of the converter's
processes once a second (every process whose args contain the work directory), then reads the unified log for
sandbox denials of sysctl reads in the run's time window. Writes counts, seconds, MiB and sysctl names only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from reader.ingest.host import cli, sandbox


def sample_peak(work: str, stop: threading.Event, peak: list[int]) -> None:
    while not stop.is_set():
        ps = subprocess.run(["/bin/ps", "-A", "-o", "rss=,args="], capture_output=True, text=True).stdout
        total = sum(int(line.split(None, 1)[0]) for line in ps.splitlines() if work + "/" in line)
        peak[0] = max(peak[0], total)
        stop.wait(1.0)


def main() -> None:
    profile, pdf, data, work_s, out = sys.argv[1:6]
    sandbox.PROFILE = Path(profile).resolve()  # type: ignore[misc]
    work = Path(os.path.realpath(work_s))
    shutil.rmtree(work, ignore_errors=True)
    (work / "in").mkdir(parents=True)
    shutil.copyfile(pdf, work / "in" / "material.pdf")
    peak = [0]
    stop = threading.Event()
    sampler = threading.Thread(target=sample_peak, args=(str(work), stop, peak), daemon=True)
    began = datetime.now()
    t0 = time.monotonic()
    sampler.start()
    try:
        members, _ = cli.convert_members(work, cli.real_deps(Path(data)), lambda _line: None)
    finally:
        stop.set()
        sampler.join()
    wall = time.monotonic() - t0
    shutil.rmtree(work, ignore_errors=True)
    manifest = json.loads(members["manifest.json"])
    log = subprocess.run(
        ["/usr/bin/log", "show", "--style", "compact", "--start", began.strftime("%Y-%m-%d %H:%M:%S"),
         "--predicate", 'eventMessage CONTAINS "sysctl"'],
        capture_output=True, text=True,
    ).stdout
    denied = sorted({m.group(1) for m in re.finditer(r"deny\(1\) sysctl-read (\S+)", log)})
    result = {
        "profile": Path(profile).name,
        "page_count": manifest["page_count"],
        "pages_converted": manifest["pages_converted"],
        "span_count": manifest["span_count"],
        "wall_seconds": round(wall, 1),
        "pages_per_minute": round(manifest["page_count"] / wall * 60, 1),
        "converter_peak_rss_mib": round(peak[0] / 1024, 1),
        "denied_sysctl_names": denied,
        "log_lines_scanned": len(log.splitlines()),
    }
    Path(out).write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
