"""SRV-PLAN / WEB-PLAN live check against the running stack: preview, confirm without compile, replay, re-plan.

Run by e2e/live/plan.sh through the server environment (`uv run --project server`). Prints one `PASS name` /
`FAIL name` line per step and exits with the number of failed steps. Spends nothing: `compile_first` is always false.

Owner session (paired change, 2026-11-23, at the coordinator's request while the owner holds a real Safari passkey):
this check creates **one** session row for the owner, with a fresh random token whose sha256 is the only thing sent
to the database, and deletes **exactly that row** (by `id_sha256`) afterwards, also on failure. It creates no
passkey, and never touches the owner's other sessions or credentials (no `--reset-owner-passkeys`, no owner-wide
DELETE). The cookie lives only in this process's memory and in the screenshot helper's environment.

Output: OUT/plan-results.json with numbers and ids only. Lesson titles are book text: they are never printed or
written by this script (the screenshots, outside the repository, show them).
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from passkey_check import psql  # noqa: E402
from web_live import fetch_root, node_path  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
PILOT_PREFIX = "61e8d4c390ba"  # pilot/bashir/plan.yaml sha256 prefix
SESSION_COOKIE = "__Host-reader_session"
SESSION_TTL_MIN = 30
TIMEOUT = 60.0
failures = 0


def report(name: str, ok: bool, detail: str = "") -> bool:
    global failures
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"\t{detail}" if detail and not ok else ""))
    failures += 0 if ok else 1
    return ok


def session_sql(token: str) -> tuple[str, str]:
    """The INSERT and the DELETE of this run's one owner session, by its id hash only."""
    h = hashlib.sha256(token.encode("utf-8")).hexdigest()
    insert = (
        "INSERT INTO sessions (id_sha256, owner_id, expires_at) "
        f"SELECT decode('{h}', 'hex'), id, now() + interval '{SESSION_TTL_MIN} minutes' FROM users WHERE role = 'owner';"
    )
    delete = f"DELETE FROM sessions WHERE id_sha256 = decode('{h}', 'hex');"
    return insert, delete


def check_api(c: httpx.Client, out: Path) -> dict[str, Any]:
    results: dict[str, Any] = {}
    r = c.get("/api/materials")
    mats = r.json().get("materials", []) if r.status_code == 200 else []
    pilot = next((m for m in mats if m.get("sha256", "").startswith(PILOT_PREFIX)), None)
    if not report("plan: the pilot is listed as imported", pilot is not None and pilot.get("status") == "imported"):
        return results
    assert pilot is not None
    mid = pilot["id"]
    results["material_id"] = mid
    today = dt.date.today().isoformat()
    body = {"material_id": mid, "total_weeks": 12, "minutes_per_day": 60, "today": today}
    r = c.post("/api/plans/preview", json=body)
    ok = report("plan: preview 12 weeks × 60 min answers 200", r.status_code == 200, str(r.status_code))
    if not ok:
        return results
    p = r.json()
    s, e = p["summary"], p["estimate"]
    results["preview"] = {
        k: s[k] for k in ("lesson_count", "fixed_count", "new_count", "study_days", "days_left", "minutes_mean",
                          "minutes_max", "required_minutes", "fits", "reasons")
    } | {k: e[k] for k in ("course_low_usd", "course_high_usd", "mean_lesson_kchars", "extrapolated")}
    results["preview"]["first_lesson"] = e["first_lesson"]
    report("plan: 12 weeks spread over 84 days, one lesson a day", s["study_days"] == 84 and s["new_count"] <= 84)
    report("plan: every page planned once", [pg for x in p["lessons"] for pg in range(x["pages"][0], x["pages"][1] + 1)]
           == list(range(1, p["lessons"][-1]["pages"][1] + 1)))
    report("plan: the estimate is flagged as an extrapolation", e["extrapolated"] is True)
    timings = []
    for _ in range(20):
        r2 = c.post("/api/plans/preview", json=body)
        timings.append(r2.elapsed.total_seconds() * 1000)
    timings.sort()
    results["preview_p95_ms"] = round(timings[18], 1)
    report("plan: preview p95 ≤ 500 ms over 20 calls (NFR-PLAN-1)", timings[18] <= 500, f"{timings[18]:.0f} ms")
    weeks = 12
    if not s["fits"]:
        # The whole pilot at the owner's rate needs more than 84 hours: the plan must say so and suggest a fit.
        sug = s["suggest"] or {}
        results["suggest_at_12_weeks"] = sug
        if not report("plan: 12 weeks × 60 min does not fit and a weeks suggestion is given",
                      isinstance(sug.get("total_weeks"), int), ",".join(s["reasons"])):
            return results
        weeks = sug["total_weeks"]
        body = body | {"total_weeks": weeks}
        r = c.post("/api/plans/preview", json=body)
        p = r.json()
        results["preview_suggested"] = {k: p["summary"][k] for k in ("lesson_count", "new_count", "study_days",
                                                                     "minutes_mean", "minutes_max", "fits")}
        if not report(f"plan: the suggested {weeks} weeks fit", r.status_code == 200 and p["summary"]["fits"]):
            return results
    results["weeks"] = weeks
    already = c.get("/api/plans", params={"material_id": mid})
    results["stored_before"] = already.status_code
    r = c.post("/api/plans/confirm", json=body | {"plan_sha256": p["plan_sha256"], "compile_first": False})
    ok = report("plan: confirm without compile answers 200", r.status_code == 200, f"{r.status_code} {r.text[:80]}")
    if not ok:
        return results
    conf = r.json()
    results["confirm"] = {k: conf[k] for k in ("course_id", "plan_course_id", "created", "removed", "replayed")} | {
        "lessons": len(conf["lessons"]), "compile": conf["compile"]}
    report("plan: no compile run was started", conf["compile"] is None)
    report("plan: ordinals follow the plan", [x["ordinal"] for x in conf["lessons"]] == list(range(1, len(conf["lessons"]) + 1)))
    r = c.post("/api/plans/confirm", json=body | {"plan_sha256": p["plan_sha256"], "compile_first": False})
    report("plan: the same confirm replays", r.status_code == 200 and r.json()["replayed"] is True
           and r.json()["lessons"] == conf["lessons"])
    r = c.get("/api/plans", params={"material_id": mid})
    report("plan: GET /api/plans returns the stored plan", r.status_code == 200 and r.json()["plan_sha256"] == p["plan_sha256"])
    # re-plan: 4 more weeks, the deadline moves from the stored start
    body16 = body | {"total_weeks": weeks + 4}
    r = c.post("/api/plans/preview", json=body16)
    ok = report("plan: re-plan preview with 4 more weeks answers 200", r.status_code == 200)
    if ok:
        p16 = r.json()
        results["replan_preview"] = {k: p16["summary"][k] for k in ("lesson_count", "fixed_count", "new_count", "study_days", "fits")}
        r = c.post("/api/plans/confirm", json=body16 | {"plan_sha256": p16["plan_sha256"], "compile_first": False})
        ok = report("plan: re-plan confirm answers 200", r.status_code == 200, str(r.status_code))
        if ok:
            rc = r.json()
            results["replan_confirm"] = {k: rc[k] for k in ("created", "removed", "replayed")} | {"lessons": len(rc["lessons"])}
            got = c.get("/api/plans", params={"material_id": mid}).json()
            report("plan: the stored plan is the re-planned one, start date kept",
                   got["inputs"]["total_weeks"] == weeks + 4 and got["inputs"]["start_date"] == p["inputs"]["start_date"])
    # back to the first confirmed plan, so the stack is left with it
    p12 = c.post("/api/plans/preview", json=body).json()
    r = c.post("/api/plans/confirm", json=body | {"plan_sha256": p12["plan_sha256"], "compile_first": False})
    report(f"plan: back to {weeks} weeks", r.status_code == 200)
    if r.status_code == 200:
        results["final"] = {k: r.json()[k] for k in ("created", "removed")} | {"lessons": len(r.json()["lessons"])}
    return results


def shots(url: str, spki: str, root_pem: Path, token: str, out: Path, shots_dir: str, weeks: int) -> None:
    env = {
        "PLAN_WEEKS": str(weeks),
        "PATH": node_path(),
        "HOME": os.environ.get("HOME", ""),
        "READER_URL": url,
        "READER_CA_SPKI": spki,
        "NODE_EXTRA_CA_CERTS": str(root_pem.resolve()),
        "READER_SESSION": token,
        "PLAN_SHOTS": shots_dir,
        "NODE_PATH": str(REPO / "web" / "node_modules"),
    }
    done = subprocess.run(["node", str(Path(__file__).with_name("plan_shots.mjs"))], env=env, capture_output=True,
                          timeout=300, check=False, cwd=REPO / "web")
    for line in done.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith(("PASS ", "FAIL ")):
            report(line[5:], line.startswith("PASS "))
    if done.returncode != 0 and not done.stdout:
        report("plan: screenshots taken", False, done.stderr.decode("utf-8", "replace")[-200:])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="reader")
    parser.add_argument("--url", default="https://localhost")
    parser.add_argument("--out", required=True)
    parser.add_argument("--shots", default="", help="directory for screenshots (outside the repository)")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    root_pem = out / "caddy-root.crt"
    spki = fetch_root(args.project, root_pem)
    if not report("plan: Caddy local root fetched", spki is not None):
        return failures
    assert spki is not None
    count_sql = (
        "SELECT (SELECT count(*) FROM webauthn_credentials c JOIN users u ON u.id = c.owner_id WHERE u.role = 'owner')"
        " || ' ' || (SELECT count(*) FROM sessions s JOIN users u ON u.id = s.owner_id WHERE u.role = 'owner'"
        " AND s.revoked_at IS NULL AND s.expires_at > now());"
    )
    _, before = psql(args.project, count_sql)
    token = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    insert, delete = session_sql(token)
    code, _ = psql(args.project, insert)
    if not report("plan: one owner session created for this run (by id)", code == 0):
        return failures
    results: dict[str, Any] = {}
    try:
        with httpx.Client(base_url=args.url, verify=str(root_pem), timeout=TIMEOUT,
                          cookies={SESSION_COOKIE: token}, headers={"Origin": args.url}) as c:
            report("plan: the session is the owner's", c.get("/api/me").json().get("role") == "owner")
            results = check_api(c, out)
        if args.shots:
            shots(args.url, spki, root_pem, token, out, args.shots, int(results.get("weeks", 12)))
    finally:
        code, _ = psql(args.project, delete)
        report("plan: this run's session deleted (by id; no other session or passkey touched)", code == 0)
        del token
        _, after = psql(args.project, count_sql)
        results["owner_passkeys_and_live_sessions"] = {"before": before, "after": after}
        report("plan: the owner's passkeys and live sessions are unchanged", before == after and before != "",
               f"{before} -> {after}")
    (out / "plan-results.json").write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", "utf-8")
    return failures


if __name__ == "__main__":
    sys.exit(main())
