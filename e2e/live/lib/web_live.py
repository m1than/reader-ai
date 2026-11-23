"""Browser part of the live checks: prepares the learner `live-check`, runs the Playwright suite, guards the artifacts.

Run by e2e/live/verify-all.sh through the server environment (`uv run --project server`). Prints one
`PASS name (Ns)` / `FAIL name` / `SKIP name` line per check and exits with the number of failures.

Steps:
  1. Caddy's local root (public) is fetched, and its SPKI hash computed so Chromium trusts it without certificate
     errors (WebAuthn refuses pages with certificate errors).
  2. live-check is reset (credentials and sessions) and the fixture lesson F01 loaded into its account.
  3. A bootstrap token is issued inside the api container (as passkey_check.py does) and handed to the Playwright
     process through its environment only; the full environment of this process is never passed on.
  4. After the run, every artifact is scanned for the token (zip members included). Any hit is scrubbed and is a FAIL.
     Cookie and session-cookie values in the trace are redacted.

--owner (verify-all.sh --slice; owner's choice 2026-11-23): the same steps for the owner instead of live-check, then
the slice suite (e2e/web/live/owner.slice.ts). No reset and no fixture: the owner's link is refused once the owner
has a passkey, so a second run needs --reset-owner-passkeys, which deletes every passkey and session of the owner
(including a real one from Safari). The course plan and questions-v2 are handed over as JSON files in the out
directory, which is never committed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

sys.path.insert(0, str(Path(__file__).resolve().parent))
from passkey_check import EXISTS_SQL, RESET_SQL, bootstrap_token, compose, psql  # noqa: E402

HANDLE = "live-check"
PLAYWRIGHT_TIMEOUT_S = 20 * 60  # whole browser suite; each step inside is bounded by its own timeout
SLICE_TIMEOUT_S = 220 * 60  # --owner: owner.slice.ts budgets import 10 + embed 80 + bench 20 + compile 75 + 11 + ui 10
REPO = Path(__file__).resolve().parents[3]
OWNER_PASSKEYS_SQL = (
    "SELECT count(*) FROM webauthn_credentials c JOIN users u ON u.id = c.owner_id WHERE u.role = 'owner';"
)
OWNER_RESET_SQL = (
    "DELETE FROM sessions WHERE owner_id = (SELECT id FROM users WHERE role = 'owner'); "
    "DELETE FROM webauthn_credentials WHERE owner_id = (SELECT id FROM users WHERE role = 'owner');"
)
COOKIE_RES = [
    re.compile(rb'("name"\s*:\s*"(?:[Cc]ookie|[Ss]et-[Cc]ookie)"\s*,\s*"value"\s*:\s*")[^"]*'),
    re.compile(rb'("name"\s*:\s*"__Host-[^"]*"\s*,\s*"value"\s*:\s*")[^"]*'),
]
failures = 0


def line(status: str, name: str, extra: str = "") -> None:
    """One `<STATUS> browser: <label>` line; any detail follows a TAB, so the label stays exact (OPS-DOD FR-DOD-4)."""
    global failures
    if status == "FAIL":
        failures += 1
    print(f"{status} {name}" + (f"\t{extra}" if extra else ""), flush=True)


def node_path() -> str:
    preferred = "/opt/homebrew/opt/node@26/bin"
    return f"{preferred}:{os.environ.get('PATH', '')}" if Path(preferred).is_dir() else os.environ.get("PATH", "")


def _spki(pem: bytes) -> str:
    key = x509.load_pem_x509_certificate(pem).public_key()
    return base64.b64encode(hashlib.sha256(key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)).digest()).decode()


def fetch_root(project: str, dest: Path) -> str | None:
    """SPKI hashes of Caddy's local root and intermediate (public certificates), comma-separated.

    Chromium matches --ignore-certificate-errors-spki-list against the chain the server sends, which holds the leaf
    and the intermediate but not the root; the root's hash alone was not enough (measured: ERR_CERT_AUTHORITY_INVALID).
    """
    pems: list[bytes] = []
    for name in ("root", "intermediate"):
        done = compose(project, "exec", "-T", "caddy", "cat", f"/data/caddy/pki/authorities/local/{name}.crt")
        if done.returncode != 0 or not done.stdout.strip():
            return None
        pems.append(done.stdout)
    dest.write_bytes(pems[0])  # the root, for Node's verifier (NODE_EXTRA_CA_CERTS)
    return ",".join(_spki(p) for p in pems)


def load_fixture(project: str) -> bool:
    done = compose(project, "exec", "-T", "api", "python", "-I", "-m", "reader.lessons.fixture", "load",
                   "--handle", HANDLE, timeout=120)
    return done.returncode == 0


def redact_cookies(path: Path) -> None:
    """Rewrite a trace zip with Cookie / Set-Cookie / __Host-* values blanked."""
    tmp = path.with_suffix(".tmp")
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info)
            if info.filename.endswith((".trace", ".network", ".json", ".jsonl")):
                for pattern in COOKIE_RES:
                    data = pattern.sub(rb"\1[redacted]", data)
            dst.writestr(info, data)
    tmp.replace(path)


def scrub_token(out: Path, token: bytes) -> list[str]:
    """Paths (or zip members) that contained the token; text files are scrubbed in place, the rest deleted."""
    hits: list[str] = []
    for f in sorted(p for p in out.rglob("*") if p.is_file()):
        raw = f.read_bytes()
        if zipfile.is_zipfile(f):
            with zipfile.ZipFile(f) as z:
                if any(token in z.read(m) for m in z.namelist()):
                    hits.append(str(f.relative_to(out)))
                    f.unlink()
            continue
        if token in raw:
            hits.append(str(f.relative_to(out)))
            if f.suffix in {".log", ".json", ".txt", ".md"}:
                f.write_bytes(raw.replace(token, b"[redacted]"))
            else:
                f.unlink()
    return hits


def walk_results(node: dict[str, object], prefix: str = "") -> list[tuple[str, str, float]]:
    found: list[tuple[str, str, float]] = []
    for spec in node.get("specs", []):  # type: ignore[union-attr]
        title = str(spec["title"])  # type: ignore[index]
        for t in spec["tests"]:  # type: ignore[index]
            results = t.get("results") or [{}]
            r = results[-1]
            status = {"passed": "PASS", "skipped": "SKIP"}.get(str(r.get("status", t.get("status"))), "FAIL")
            found.append((status, prefix + title, float(r.get("duration", 0)) / 1000))
    for suite in node.get("suites", []):  # type: ignore[union-attr]
        found += walk_results(suite)  # type: ignore[arg-type]
    return found


def prepare_owner(project: str, reset: bool) -> bool:
    """The owner may receive a bootstrap link only without a passkey (SRV-AUTH owner_has_credential)."""
    code, count = psql(project, OWNER_PASSKEYS_SQL)
    if code != 0 or not count.isdigit():
        line("FAIL", "browser: owner passkeys counted")
        return False
    if count != "0" and not reset:
        line("FAIL", "browser: the owner has no passkey yet",
             f"found {count}; --reset-owner-passkeys deletes them all")
        return False
    if count != "0":
        code, _ = psql(project, OWNER_RESET_SQL)
        line("PASS" if code == 0 else "FAIL", "browser: owner passkeys and sessions deleted", f"{count}")
        return code == 0
    line("PASS", "browser: the owner has no passkey yet")
    return True


def slice_inputs(out: Path, steps: str) -> dict[str, str]:
    """The pilot plan (pilot/bashir/course.yaml) and questions-v2 as JSON for the slice suite; ids, pages, questions."""
    from ruamel.yaml import YAML

    yaml = YAML(typ="safe")
    plan = yaml.load((REPO / "pilot" / "bashir" / "course.yaml").read_text("utf-8"))
    questions = yaml.load((REPO / "spikes" / "embeddings-local" / "questions-v2.yaml").read_text("utf-8"))
    plan_path, questions_path = out / "slice-plan.json", out / "slice-questions.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, default=str), "utf-8")
    picked = [{"id": q["id"], "gold_pages": q["gold_pages"], "question": q["question"]} for q in questions["questions"]]
    questions_path.write_text(json.dumps(picked, ensure_ascii=False), "utf-8")
    return {
        "READER_SLICE_STEPS": steps,
        "READER_SLICE_PLAN": str(plan_path.resolve()),
        "READER_SLICE_QUESTIONS": str(questions_path.resolve()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--url", default="https://localhost")
    parser.add_argument("--out", required=True)
    parser.add_argument("--suite", required=True, help="e2e/web/live")
    parser.add_argument("--owner", action="store_true", help="owner session and the slice suite (verify-all --slice)")
    parser.add_argument("--reset-owner-passkeys", action="store_true",
                        help="with --owner: delete every passkey and session of the owner first")
    parser.add_argument("--slice-steps", default="routes", help="with --owner: comma-separated slice steps")
    args = parser.parse_args()
    out = Path(args.out)
    suite = Path(args.suite)
    out.mkdir(parents=True, exist_ok=True)

    root_pem = out / "caddy-root.crt"  # public certificate
    spki = fetch_root(args.project, root_pem)
    line("PASS" if spki else "FAIL", "browser: Caddy local root fetched, SPKI pinned for Chromium")
    if not spki:
        return failures

    for stale in ("playwright.json", "slice-results.json"):  # never report a previous run's results
        (out / stale).unlink(missing_ok=True)
    if args.owner:
        if not prepare_owner(args.project, args.reset_owner_passkeys):
            return failures
        try:
            run_suite(args, out, suite, spki, root_pem, "owner")
        finally:
            # The virtual passkey dies with the browser: remove it and the run's sessions so the owner's own
            # `reader bootstrap` (Safari) is not refused with owner_has_credential (deep-reviewer, 2026-11-23).
            code, _ = psql(args.project, OWNER_RESET_SQL)
            line("PASS" if code == 0 else "FAIL", "browser: the run's owner passkey and sessions removed")
        return failures
    code, count = psql(args.project, EXISTS_SQL)
    if code != 0 or count != "1":
        line("FAIL", "browser: live-check is a configured learner")
        return failures
    code, _ = psql(args.project, RESET_SQL)
    line("PASS" if code == 0 else "FAIL", "browser: live-check reset for a fresh passkey")
    line("PASS" if load_fixture(args.project) else "FAIL", "browser: fixture lesson F01 loaded for live-check")
    return run_suite(args, out, suite, spki, root_pem, "learner")


def run_suite(args: argparse.Namespace, out: Path, suite: Path, spki: str, root_pem: Path, who: str) -> int:
    """Issue the link, run Playwright with the token in its environment only, then guard every artifact."""
    extra: dict[str, str] = {}
    if who == "owner":
        extra = slice_inputs(out, args.slice_steps)
        token = bootstrap_token(args.project, args.url, handle=None)
        config, budget = "playwright.slice.config.ts", SLICE_TIMEOUT_S
    else:
        token = bootstrap_token(args.project, args.url)
        config, budget = "playwright.live.config.ts", PLAYWRIGHT_TIMEOUT_S

    line("PASS" if token else "FAIL", "browser: bootstrap link issued (not printed)")
    if not token:
        return failures
    token_bytes = token.encode()

    env = {
        "PATH": node_path(),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "READER_URL": args.url,
        "READER_LIVE_OUT": str(out.resolve()),
        "READER_CA_SPKI": spki,
        "NODE_EXTRA_CA_CERTS": str(root_pem.resolve()),
        "READER_BOOTSTRAP_TOKEN": token,
        **extra,
    }
    del token
    started = time.monotonic()
    with (out / "playwright.log").open("wb") as log:
        # Own process group, so a timeout stops Playwright's runner and browsers too, not only npx.
        proc = subprocess.Popen(["npx", "playwright", "test", "-c", config], cwd=suite, env=env, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
        try:
            rc = proc.wait(timeout=budget)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            rc = -1
    env.pop("READER_BOOTSTRAP_TOKEN")
    elapsed = time.monotonic() - started

    trace = out / "trace.zip"
    if trace.exists():
        redact_cookies(trace)
    hits = scrub_token(out, token_bytes)
    del token_bytes
    line("FAIL" if hits else "PASS", "browser: bootstrap token appears in no artifact",
         f"(found and removed in: {', '.join(hits)})" if hits else "")

    report = out / "playwright.json"
    if not report.exists():
        line("FAIL", f"browser: Playwright produced no report (exit {rc}, {elapsed:.0f}s)")
        return failures
    data = json.loads(report.read_text("utf-8"))
    walked = walk_results(data)
    for status, title, seconds in walked:
        line(status, f"browser: {title}", f"({seconds:.1f}s)")
    if rc == -1:
        line("FAIL", "browser: suite exceeded its time budget", f"{budget}s")
    elif rc != 0 and not any(status == "FAIL" for status, _, _ in walked):
        line("FAIL", "browser: Playwright exited non-zero without a failing test", f"exit {rc}; see playwright.log")
    return failures


if __name__ == "__main__":
    sys.exit(main())
