"""Stage-1 passkey flow for the learner `live-check` against the running stack (SRV-AUTH, S1.live).

Run by e2e/live/stage-1.sh through the server environment (`uv run --project server`), which has httpx, cbor2 and
cryptography. Prints one `PASS name` / `FAIL name` line per step and exits with the number of failed steps.

Secrets: the bootstrap link is captured from `reader-server bootstrap-link` inside the api container and kept in
memory; it is never printed, logged or written. Cookies live only in the httpx client's memory (no cookie file).
The software authenticator's key is generated per run and discarded (no committed key).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "server" / "tests" / "auth"))
from softauthn import SoftAuthenticator, unb64url  # noqa: E402

HANDLE = "live-check"
RESET_SQL = (
    "DELETE FROM sessions WHERE owner_id = (SELECT id FROM users WHERE handle = 'live-check'); "
    "DELETE FROM webauthn_credentials WHERE owner_id = (SELECT id FROM users WHERE handle = 'live-check');"
)
EXISTS_SQL = "SELECT count(*) FROM users WHERE handle = 'live-check' AND role = 'learner' AND disabled_at IS NULL;"
TIMEOUT = 30.0
failures = 0


def report(name: str, ok: bool, detail: str = "") -> bool:
    global failures
    if ok:
        print(f"PASS {name}")
    else:
        failures += 1
        print(f"FAIL {name}" + (f": {detail}" if detail else ""))
    return ok


def compose(project: str, *args: str, timeout: float = 60) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["docker", "compose", "-p", project, *args], capture_output=True, timeout=timeout, check=False
    )


def psql(project: str, sql: str) -> tuple[int, str]:
    done = compose(project, "exec", "-T", "postgres", "psql", "-U", "postgres", "-d", "postgres",
                   "-v", "ON_ERROR_STOP=1", "-At", "-c", sql)
    return done.returncode, done.stdout.decode("utf-8", "replace").strip()


def bootstrap_token(project: str, url: str, handle: str | None = HANDLE) -> str | None:
    """The token from the link; the link itself never leaves this function. `handle=None` is the owner's link,
    as `reader bootstrap` issues it (FR-DEPLOY-11); it is refused once the owner has a passkey."""
    argv = ["exec", "-T", "api", "reader-server", "bootstrap-link"] + (["--handle", handle] if handle else [])
    done = compose(project, *argv)
    if done.returncode != 0:
        return None
    lines = done.stdout.decode("utf-8", "replace").strip().splitlines()
    prefix = f"{url}/api/auth/bootstrap#"
    if len(lines) != 1 or not lines[0].startswith(prefix):
        return None
    token = lines[0][len(prefix):]
    return token or None


def post(c: httpx.Client, path: str, body: dict[str, Any]) -> httpx.Response:
    return c.post(path, json=body)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--url", default="https://localhost")
    parser.add_argument("--cacert", required=True)
    args = parser.parse_args()

    code, out = psql(args.project, EXISTS_SQL)
    if not report("live-check is a configured learner", code == 0 and out == "1",
                  "add users: [{handle: live-check, role: learner}] to reader.yaml and run `reader up`"):
        return failures
    code, _ = psql(args.project, RESET_SQL)
    if not report("live-check reset (credentials and sessions deleted)", code == 0):
        return failures
    token = bootstrap_token(args.project, args.url)
    if not report("bootstrap link issued for live-check (not printed)", token is not None):
        return failures

    auth = SoftAuthenticator.random()
    headers = {"Origin": args.url}
    with httpx.Client(base_url=args.url, verify=args.cacert, headers=headers, timeout=TIMEOUT) as c:
        options = post(c, "/api/auth/bootstrap/options", {"token": token})
        del token
        if not report("bootstrap options", options.status_code == 200, f"HTTP {options.status_code}"):
            return failures
        opts = options.json()
        user_handle = unb64url(opts["user"]["id"])
        credential = auth.register(rp_id=opts["rp"]["id"], origin=args.url, challenge=unb64url(opts["challenge"]))
        verify = post(c, "/api/auth/bootstrap/verify", credential)
        report("registration with a per-run software passkey", verify.status_code == 200,
               f"HTTP {verify.status_code}")

        me = c.get("/api/me")
        report("/api/me shows learner live-check",
               me.status_code == 200 and me.json().get("role") == "learner" and me.json().get("handle") == HANDLE,
               f"HTTP {me.status_code}")

        signout = c.post("/api/auth/signout")
        report("sign-out", signout.status_code == 204, f"HTTP {signout.status_code}")
        c.cookies.clear()
        after = c.get("/api/me")
        report("signed out: /api/me is 401", after.status_code == 401, f"HTTP {after.status_code}")

        options = post(c, "/api/auth/signin/options", {})
        if report("sign-in options", options.status_code == 200, f"HTTP {options.status_code}"):
            opts = options.json()
            assertion = auth.assertion(rp_id=opts.get("rpId", "localhost"), origin=args.url,
                                       challenge=unb64url(opts["challenge"]), user_handle=user_handle)
            signin = post(c, "/api/auth/signin/verify", assertion)
            report("sign-in with the same passkey", signin.status_code == 200, f"HTTP {signin.status_code}")
            again = c.get("/api/me")
            report("signed in again: /api/me is 200", again.status_code == 200, f"HTTP {again.status_code}")
        c.post("/api/auth/signout")
    return failures


if __name__ == "__main__":
    sys.exit(main())
