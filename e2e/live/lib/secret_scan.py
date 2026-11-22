"""ADR-0026 `verified_by`: the delivered DB password appears in no Docker record (stage-1 live check).

The value is read from postgres's /run/secrets/db-password into this process's memory and used only as a byte
pattern. It is never printed, logged or written. Prints one `PASS name` / `FAIL name` line per record kind and
exits with the number of failures.

Records scanned, per ADR-0026 and OPS-DEPLOY FR-DEPLOY-10: `docker inspect` of every container of the project,
`docker export` of every container (streamed), `docker events` of the last 24 h, and the Docker Desktop log
directory on this Mac.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

DESKTOP_LOGS = Path.home() / "Library" / "Containers" / "com.docker.docker" / "Data" / "log"
CHUNK = 1 << 20
failures = 0


def report(name: str, ok: bool, detail: str = "") -> None:
    global failures
    if ok:
        print(f"PASS {name}")
    else:
        failures += 1
        print(f"FAIL {name}" + (f": {detail}" if detail else ""))


def run(*argv: str, timeout: float = 120) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(list(argv), capture_output=True, timeout=timeout, check=False)


def stream_contains(argv: list[str], needle: bytes) -> bool | None:
    """True if the command's stdout contains needle; None if the command failed. Streams in chunks."""
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        assert proc.stdout is not None
        tail = b""
        found = False
        while chunk := proc.stdout.read(CHUNK):
            window = tail + chunk
            if needle in window:
                found = True
                proc.kill()
                break
            tail = window[-(len(needle) - 1):] if len(needle) > 1 else b""
        code = proc.wait()
    if found:
        return True
    return None if code != 0 else False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    args = parser.parse_args()

    read = run("docker", "compose", "-p", args.project, "exec", "-T", "postgres", "cat", "/run/secrets/db-password")
    needle = read.stdout.strip()
    if read.returncode != 0 or len(needle) < 16:
        report("db-password readable from postgres for the scan", False, "missing or too short")
        return failures

    ids = run("docker", "compose", "-p", args.project, "ps", "-aq").stdout.decode().split()
    report("project containers found", bool(ids))

    inspect = run("docker", "inspect", *ids) if ids else None
    report("no secret in docker inspect", inspect is not None and inspect.returncode == 0
           and needle not in inspect.stdout)

    leaked = []
    for cid in ids:
        hit = stream_contains(["docker", "export", cid], needle)
        if hit is not False:
            leaked.append(cid[:12] + ("" if hit else " (export failed)"))
    report("no secret in docker export (container layers)", not leaked, ", ".join(leaked))

    events = run("docker", "events", "--since", "24h", "--until", "0s", "--format", "{{json .}}", timeout=60)
    report("no secret in docker events (24 h)", events.returncode == 0 and needle not in events.stdout)

    if DESKTOP_LOGS.is_dir():
        hits = []
        for path in DESKTOP_LOGS.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink() and needle in path.read_bytes():
                    hits.append(path.name)
            except OSError:
                continue
        report("no secret in Docker Desktop logs", not hits, ", ".join(hits))
    else:
        print("SKIP Docker Desktop logs (directory not found)")
    del needle
    return failures


if __name__ == "__main__":
    sys.exit(main())
