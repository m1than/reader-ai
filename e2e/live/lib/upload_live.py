"""Upload track live check (SRV-INGEST FR-INGEST-18..23, WEB-IMPORT, OPS-DEPLOY FR-DEPLOY-13) against `reader up`.

Run by e2e/live/upload.sh through the server environment (`uv run --project server`), with `reader-ingest --watch`
running (`reader up` starts it). Prints one `PASS name` / `FAIL name` line per step and exits with the number of
failed steps. Spends nothing: no model call; the synthetic PDF is VEC-INGEST-2's generator output (no book text).

Steps:
1. the converter is online;
2. the browser uploads the synthetic PDF on /import and follows it to Ready (upload_page.mjs: axe at 1280, 1024 and
   390 px, no CSP violation); the time to ready and the worker's memory.peak are recorded;
3. a small generated PDF is uploaded and cancelled: no row, no inbox file afterwards;
4. a body of exactly 209,715,200 bytes passes Caddy and the api (201), is fetched by the watcher (the fetch time is
   read from the watcher's log by upload id), fails `convert_failed` (junk pages) and is discarded;
5. 209,715,201 bytes are 413.

Owner session: one row created for this run and deleted by its id hash afterwards, as plan_live.py does. Cleanup:
only what this run created, by upload id and sha256 (rows of the synthetic material, its job keys, its inbox file
and its stored artifact). Output: OUT/upload-results.json with numbers and ids only.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from passkey_check import compose, psql  # noqa: E402
from plan_live import report, session_sql  # noqa: E402
from web_live import fetch_root, node_path  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
SYNTHETIC = REPO / "spec" / "fixtures" / "SRV-INGEST" / "synthetic.pdf"
SESSION_COOKIE = "__Host-reader_session"
CAP = 209_715_200
WATCH_LOG = Path.home() / "Library" / "Application Support" / "reader-ai" / "run" / "ingest-watch.log"
INBOX = "/var/lib/reader/blobs/inbox"
STORE = "/var/lib/reader/blobs/ingest"
SHA = re.compile(r"[0-9a-f]{64}")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def pdf(size: int) -> bytes:
    """A body that passes the magic check (VEC-INGEST-7) and is not a document: junk pages fail conversion."""
    head = b"%PDF-1.7\n%" + secrets.token_hex(16).encode() + b"\n"
    tail = b"\n%%EOF\n"
    return head + b"x" * (size - len(head) - len(tail)) + tail


def post(c: httpx.Client, body: bytes, name: str) -> httpx.Response:
    return c.post(
        "/api/materials/uploads",
        params={"language": "other"},
        content=body,
        headers={"Content-Type": "application/pdf", "X-Reader-Filename": name},
        timeout=600,
    )


def wait_state(c: httpx.Client, upload_id: str, states: set[str], limit_s: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        r = c.get(f"/api/materials/uploads/{upload_id}")
        if r.status_code == 200 and r.json().get("state") in states:
            return dict(r.json())
        time.sleep(2)
    return None


def worker_file_exists(project: str, path: str) -> bool:
    return compose(project, "exec", "-T", "worker", "test", "-e", path).returncode == 0


def memory_peak(project: str) -> int | None:
    done = compose(project, "exec", "-T", "worker", "cat", "/sys/fs/cgroup/memory.peak")
    text = done.stdout.decode("ascii", "replace").strip()
    return int(text) if done.returncode == 0 and text.isdigit() else None


def fetch_seconds(upload_id: str) -> float | None:
    """claim → fetch_done of this upload in the watcher's log (lines hold ids, sizes and timings only)."""
    if not UUID.fullmatch(upload_id) or not WATCH_LOG.exists():
        return None
    stamps: dict[str, float] = {}
    for line in WATCH_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        if f"id={upload_id}" not in line:
            continue
        for event in ("upload_claimed", "fetch_done"):
            if event in line and event not in stamps:
                stamp = line[:23]  # logging's asctime: YYYY-MM-DD HH:MM:SS,mmm
                stamps[event] = time.mktime(time.strptime(stamp[:19], "%Y-%m-%d %H:%M:%S")) + int(stamp[20:23]) / 1000
    if {"upload_claimed", "fetch_done"} <= stamps.keys():
        return round(stamps["fetch_done"] - stamps["upload_claimed"], 1)
    return None


def cleanup(project: str, upload_id: str, sha: str) -> bool:
    """Remove exactly this upload: its material rows, job keys, inbox file and stored artifact."""
    if not (UUID.fullmatch(upload_id) and SHA.fullmatch(sha)):
        return False
    _, artifacts = psql(
        project,
        f"SELECT i.artifact_id FROM ingests i JOIN materials m ON m.id = i.material_id WHERE m.sha256 = '{sha}';",
    )
    ingests = f"SELECT i.id FROM ingests i JOIN materials m ON m.id = i.material_id WHERE m.sha256 = '{sha}'"
    sql = (
        "BEGIN;"
        f"DELETE FROM search_chunks WHERE ingest_id IN ({ingests});"
        f"DELETE FROM search_embeds WHERE ingest_id IN ({ingests});"
        f"DELETE FROM ingests WHERE id IN ({ingests});"
        f"DELETE FROM materials WHERE sha256 = '{sha}';"
        f"DELETE FROM procrastinate_jobs WHERE lock IN (SELECT key FROM job_keys WHERE args->>'material_sha256' = '{sha}');"
        f"DELETE FROM job_keys WHERE args->>'material_sha256' = '{sha}';"
        f"DELETE FROM uploads WHERE id = '{upload_id}' AND sha256 = '{sha}';"
        "COMMIT;"
    )
    ok = psql(project, sql)[0] == 0
    ok &= compose(project, "exec", "-T", "worker", "rm", "-f", f"{INBOX}/{sha}.pdf").returncode == 0
    for artifact in artifacts.split():
        if SHA.fullmatch(artifact):
            ok &= compose(project, "exec", "-T", "worker", "rm", "-rf", f"{STORE}/{artifact}").returncode == 0
    return ok


def browser_upload(url: str, spki: str, root_pem: Path, token: str, out: Path) -> None:
    env = {
        "PATH": node_path(),
        "HOME": os.environ.get("HOME", ""),
        "READER_URL": url,
        "READER_CA_SPKI": spki,
        "NODE_EXTRA_CA_CERTS": str(root_pem.resolve()),
        "READER_SESSION": token,
        "UPLOAD_PDF": str(SYNTHETIC),
        "NODE_PATH": str(REPO / "web" / "node_modules"),
    }
    done = subprocess.run(["node", str(Path(__file__).with_name("upload_page.mjs"))], env=env, capture_output=True,
                          timeout=1200, check=False, cwd=REPO / "web")
    for line in done.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith(("PASS ", "FAIL ")):
            report(line[5:], line.startswith("PASS "))
    if done.returncode != 0 and not done.stdout:
        report("upload: the browser run finished", False, done.stderr.decode("utf-8", "replace")[-200:])
    (out / "upload-page.txt").write_bytes(done.stdout)


def run(c: httpx.Client, args: argparse.Namespace, spki: str, root_pem: Path, token: str, out: Path) -> dict[str, Any]:
    results: dict[str, Any] = {}
    created: list[tuple[str, str]] = []
    synthetic_sha = hashlib.sha256(SYNTHETIC.read_bytes()).hexdigest()
    try:
        converter = c.get("/api/materials/converter").json()
        report("upload: the converter is online", converter.get("state") == "online", json.dumps(converter))
        listed = c.get("/api/materials").json().get("materials", [])
        leftover = any(m.get("sha256") == synthetic_sha for m in listed)
        if not report("upload: no synthetic upload is left from an earlier run", not leftover):
            return results
        peak_before = memory_peak(args.project)
        started = time.monotonic()
        browser_upload(args.url, spki, root_pem, token, out)
        mine = next((m for m in c.get("/api/materials").json().get("materials", []) if m.get("sha256") == synthetic_sha), None)
        if mine and mine.get("upload"):
            created.append((mine["upload"]["id"], synthetic_sha))
            results["synthetic_upload_id"] = mine["upload"]["id"]
            results["synthetic_state"] = mine["upload"]["state"]
            results["synthetic_seconds_to_ready"] = round(time.monotonic() - started, 1)
            report("upload: the synthetic PDF reached ready", mine["upload"]["state"] == "ready")
        else:
            report("upload: the synthetic PDF is listed as an upload", False)
        results["worker_memory_peak_before"] = peak_before
        results["worker_memory_peak_after"] = memory_peak(args.project)

        small = pdf(4096)
        r = post(c, small, "Synthetic cancel.pdf")
        if report("upload: a small PDF is accepted (201)", r.status_code == 201, str(r.status_code)):
            uid, sha = r.json()["id"], hashlib.sha256(small).hexdigest()
            created.append((uid, sha))
            gone = c.post(f"/api/materials/uploads/{uid}/cancel")
            if gone.status_code == 409:  # converted already and failed: Discard instead
                wait_state(c, uid, {"failed"}, 300)
                gone = c.delete(f"/api/materials/uploads/{uid}")
            ok = gone.status_code == 200 and c.get(f"/api/materials/uploads/{uid}").status_code == 404
            report("upload: cancel leaves no row", ok, str(gone.status_code))
            report("upload: cancel leaves no inbox file", not worker_file_exists(args.project, f"{INBOX}/{sha}.pdf"))

        body = pdf(CAP)
        t0 = time.monotonic()
        r = post(c, body, "Synthetic cap.pdf")
        results["cap_upload_seconds"] = round(time.monotonic() - t0, 1)
        if report("upload: exactly 209,715,200 bytes pass Caddy and the api (201)", r.status_code == 201, str(r.status_code)):
            uid, sha = r.json()["id"], hashlib.sha256(body).hexdigest()
            created.append((uid, sha))
            failed = wait_state(c, uid, {"failed"}, args.cap_wait_s)
            report("upload: the cap body is fetched and fails conversion", failed is not None and failed.get("code") == "convert_failed",
                   json.dumps(failed))
            results["cap_fetch_seconds"] = fetch_seconds(uid)
            gone = c.delete(f"/api/materials/uploads/{uid}")
            report("upload: Discard removes the cap upload", gone.status_code == 200, str(gone.status_code))
            report("upload: Discard leaves no inbox file", not worker_file_exists(args.project, f"{INBOX}/{sha}.pdf"))
        del body
        r = post(c, pdf(CAP + 1), "Synthetic over.pdf")
        report("upload: 209,715,201 bytes are 413", r.status_code == 413, str(r.status_code))
    finally:
        for uid, sha in created:
            report(f"upload: cleanup of {uid[:8]}", cleanup(args.project, uid, sha))
    return results


def main() -> int:
    import plan_live  # report() counts failures in plan_live's module state

    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="reader")
    parser.add_argument("--url", default="https://localhost")
    parser.add_argument("--out", required=True)
    parser.add_argument("--cap-wait-s", type=float, default=1800.0)
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    root_pem = out / "caddy-root.crt"
    spki = fetch_root(args.project, root_pem)
    if not report("upload: Caddy local root fetched", spki is not None):
        return plan_live.failures
    assert spki is not None
    token = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    insert, delete = session_sql(token)
    if not report("upload: this run's owner session created", psql(args.project, insert)[0] == 0):
        return plan_live.failures
    results: dict[str, Any] = {}
    try:
        with httpx.Client(base_url=args.url, verify=str(root_pem), timeout=120,
                          cookies={SESSION_COOKIE: token}, headers={"Origin": args.url}) as c:
            results = run(c, args, spki, root_pem, token, out)
    finally:
        report("upload: this run's owner session deleted", psql(args.project, delete)[0] == 0)
        del token
    (out / "upload-results.json").write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return plan_live.failures


if __name__ == "__main__":
    sys.exit(main())
