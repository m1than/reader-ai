"""SRV-OBS [live] (spec/modules/SRV-OBS.yaml, test levels): the traces dashboard against `reader up`.

Run by e2e/live/obs.sh (as `python -I`). Prints one `PASS name`, `FAIL name<TAB>detail` or `SKIP name<TAB>why` line
per check, then `fails=N`; details are status codes, counts and span names only, never a cookie, token or trace
content.

Sessions: one owner row and one `live-check` learner row, each inserted and deleted by its id hash only through the
live library's psql helper; the owner's passkeys and other sessions are never touched (no OWNER_RESET_SQL).
Caddy's public root is read the way `reader trust` reads it and used for this run's TLS only.

Zero credit unless --with-credit: then one tutor question carrying a canary is asked (it stays in the owner's tutor
history), and its trace is checked for the job → agent.call → claude_code.interaction → claude_code.llm_request
chain, VEC-OBS-2's keys and the canary's absence (the live stack has no stub provider).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from ruamel.yaml import YAML

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from passkey_check import psql  # noqa: E402
from plan_live import session_sql  # noqa: E402
import web_live  # noqa: E402

DASH = "https://localhost:8443"
APP = "https://localhost"
COOKIE = "__Host-reader_session"
LEARNER = "live-check"
SESSION_TTL_MIN = 30
NFR_OBS_1_S = 30  # SRV-OBS NFR-OBS-1: a finished job's trace is visible within 30 s
SAMPLE = 20  # summary trace ids opened against MLflow
VEC_OBS_2 = HERE.parents[2] / "spec" / "fixtures" / "SRV-OBS" / "cli-span-keys.yaml"
CHAIN = ("job tutor.answer", "agent.call", "claude_code.interaction", "claude_code.llm_request")
OURS_RESOURCE = {"service.name", "service.version"}  # FR-OBS-1's resource on our spans
BLOCKED = (
    "/v1/traces",
    "/V1/traces",
    "//gateway/x",
    "/mcp",
    "/api/2.0/mlflow/gateway/x",
    "/api/3.0/mlflow/gateway/secrets/create",
    "/ajax-api//3.0/mlflow/assistant/x",
    "/ajax-api/3.0/mlflow/assistant/sessions/abc/stream",
    "/ajax-api/2.0/mlflow/gateway-proxy/v1/chat",
)
fails = 0


def rep(ok: bool, name: str, detail: str = "") -> bool:
    global fails
    fails += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"\t{detail}" if detail and not ok else ""), flush=True)
    return ok


def skip(name: str, why: str) -> None:
    """A check that could not run on this stack's data: counted apart by obs.sh, never a silent pass."""
    print(f"SKIP {name}\t{why}", flush=True)


def info(obj: Any) -> None:
    print(f"INFO {json.dumps(obj)}", flush=True)


def _token() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def learner_sql(token: str) -> tuple[str, str]:
    """The INSERT and the DELETE of this run's learner session, by its id hash only."""
    h = hashlib.sha256(token.encode("utf-8")).hexdigest()
    insert = (
        "INSERT INTO sessions (id_sha256, owner_id, expires_at) "
        f"SELECT decode('{h}', 'hex'), id, now() + interval '{SESSION_TTL_MIN} minutes' FROM users "
        f"WHERE handle = '{LEARNER}' AND role = 'learner';"
    )
    return insert, f"DELETE FROM sessions WHERE id_sha256 = decode('{h}', 'hex');"


def one_row(project: str, sql: str) -> bool:
    """The statement ran and touched exactly one row (psql's command tag)."""
    code, out = psql(project, sql)
    return code == 0 and out.strip().splitlines()[-1:] in (["INSERT 0 1"], ["DELETE 1"])


def gate_checks(c: httpx.Client, owner: str, learner: str) -> None:
    def st(path: str, cookie: str | None = None, method: str = "GET", **headers: str) -> int:
        h = {k.replace("_", "-"): v for k, v in headers.items()}
        if cookie:
            h["Cookie"] = f"{COOKIE}={cookie}"
        return c.request(method, DASH + path, headers=h).status_code

    rep(st("/") == 401, "8443 / without a cookie is 401", str(st("/")))
    rep(st("/", learner) == 403, "8443 / with a learner session is 403", str(st("/", learner)))
    s = st("/", owner)
    rep(200 <= s < 300, "8443 / with the owner session is 2xx", str(s))
    cases = [
        ("owner + foreign Origin", {"Origin": "https://evil.example"}, "GET", "/"),
        ("owner + Origin https://localhost", {"Origin": APP}, "GET", "/"),
        ("owner + Origin in upper case", {"Origin": "HTTPS://LOCALHOST:8443"}, "POST", "/x"),
        ("owner POST without Origin", {}, "POST", "/ajax-api/2.0/mlflow/experiments/search"),
        ("owner cross-site navigation",
         {"Sec_Fetch_Site": "cross-site", "Sec_Fetch_Mode": "navigate", "Sec_Fetch_Dest": "document"}, "GET", "/"),
        ("owner same-site fetch",
         {"Sec_Fetch_Site": "same-site", "Sec_Fetch_Mode": "cors", "Sec_Fetch_Dest": "empty"}, "GET", "/"),
        ("owner same-site image",
         {"Sec_Fetch_Site": "same-site", "Sec_Fetch_Mode": "no-cors", "Sec_Fetch_Dest": "image"}, "GET", "/"),
    ]
    for name, headers, method, path in cases:
        s = st(path, owner, method, **headers)
        rep(s == 403, f"8443 {name} is 403", str(s))
    nav = {"Sec_Fetch_Site": "same-site", "Sec_Fetch_Mode": "navigate", "Sec_Fetch_Dest": "document"}
    s = st("/", owner, **nav)
    rep(200 <= s < 300, "8443 owner same-site top-level navigation is 2xx", str(s))
    for path in BLOCKED:
        r = c.get(DASH + path, headers={"Cookie": f"{COOKIE}={owner}"})
        rep(r.status_code == 404 and r.content == b"", f"8443 blocked path {path} is Caddy's empty 404", str(r.status_code))
    r = c.get(DASH + "/", headers={"Cookie": f"{COOKIE}={owner}"})
    leaked = [h for h in r.headers if h.lower() == "set-cookie" or h.lower().startswith("access-control-")]
    rep(not leaked, "8443 carries no Set-Cookie or Access-Control-*", ",".join(leaked))
    rep("content-security-policy" in r.headers, "8443 carries the dashboard CSP")


def get_trace(c: httpx.Client, owner: str, trace_id: str) -> dict[str, Any] | None:
    """MLflow's v3 trace (`trace.spans`), or None; `trace_id` with or without its `tr-` prefix."""
    tid = trace_id if trace_id.startswith("tr-") else f"tr-{trace_id}"
    r = c.get(f"{DASH}/ajax-api/3.0/mlflow/traces/get", params={"trace_id": tid},
              headers={"Cookie": f"{COOKIE}={owner}"})
    body = r.json() if r.status_code == 200 else {}
    return body if (body.get("trace") or {}).get("spans") else None


def search_traces(c: httpx.Client, owner: str, n: int = 50) -> list[dict[str, Any]]:
    r = c.post(f"{DASH}/ajax-api/3.0/mlflow/traces/search",
               headers={"Cookie": f"{COOKIE}={owner}", "Origin": DASH},
               json={"locations": [{"type": "MLFLOW_EXPERIMENT", "mlflow_experiment": {"experiment_id": "0"}}],
                     "max_results": n, "order_by": ["timestamp_ms DESC"]})
    return list(r.json().get("traces", [])) if r.status_code == 200 else []


def summary_checks(c: httpx.Client, owner: str, learner: str) -> list[str]:
    s = c.get(f"{APP}/api/obs/summary", headers={"Cookie": f"{COOKIE}={learner}"}).status_code
    rep(s == 403, "/api/obs/summary as a learner is 403", str(s))
    r = c.get(f"{APP}/api/obs/summary", headers={"Cookie": f"{COOKIE}={owner}"})
    if not rep(r.status_code == 200, "/api/obs/summary as the owner is 200", str(r.status_code)):
        return []
    body = r.json()
    ids: list[str] = []
    for run in body["compile_runs"]:
        ids += [s["trace_id"] for s in run["steps"] if s["trace_id"]]
    ids += [q["trace_id"] for q in body["tutor"]["slowest"] if q["trace_id"]]
    for group in body["failures"]["jobs"] + body["failures"]["tutor"]:
        ids += group["trace_ids"]
    ids += [row["trace_id"] for row in body["ingest"]["rows"] if row["trace_id"]]
    ids = list(dict.fromkeys(ids))
    name = "summary trace ids open existing MLflow traces (FR-OBS-10)"
    if not ids:
        skip(name, "the summary gives no trace id (no traced job in the window)")
        return []
    sample = ids[:SAMPLE]
    missing = [t for t in sample if get_trace(c, owner, t) is None]
    rep(not missing, f"the first {len(sample)} of {len(ids)} {name}", f"{len(missing)} missing")
    return sample


def dashboard_target(c: httpx.Client, owner: str) -> str | None:
    """The newest trace MLflow holds (request traces exist at zero credit): the detail, CSP and UI-write target."""
    found = search_traces(c, owner, 5)
    target = found[0]["trace_id"] if found else None
    rep(target is not None and get_trace(c, owner, target) is not None,
        "MLflow's traces/search returns a trace that opens")
    return target


def _keys(attributes: Any) -> set[str]:
    if isinstance(attributes, dict):
        return set(attributes)
    return {a.get("key", "") for a in attributes or [] if isinstance(a, dict)}


def credit_checks(c: httpx.Client, owner: str) -> None:
    """--with-credit: one tutor question (spends Agent SDK credit; the question stays in the tutor history)."""
    h = {"Cookie": f"{COOKIE}={owner}", "Origin": APP}
    lessons = c.get(f"{APP}/api/lessons", headers=h).json().get("lessons", [])
    lesson = next((x for x in lessons if x.get("status") == "published"), lessons[0] if lessons else None)
    if not rep(lesson is not None, "credit: a lesson exists to ask about"):
        return
    canary = "obscanary-" + secrets.token_hex(6)
    asked = time.time()
    r = c.post(f"{APP}/api/lessons/{lesson['id']}/ask", headers=h, json={"question": f"What is a block? ({canary})"})
    if not rep(r.status_code == 202, "credit: the question is accepted", str(r.status_code)):
        return
    qid = r.json()["question_id"]
    state = "queued"
    deadline = time.time() + 300
    while state in ("queued", "running") and time.time() < deadline:
        time.sleep(3)
        q = c.get(f"{APP}/api/lessons/{lesson['id']}/questions/{qid}", headers=h)
        state = q.json().get("state", "?") if q.status_code == 200 else "?"
    finished = time.time()
    rep(state == "answered", "credit: the question is answered", state)
    trace: dict[str, Any] | None = None
    while trace is None and time.time() < finished + NFR_OBS_1_S:
        for t in search_traces(c, owner):
            got = get_trace(c, owner, t["trace_id"])
            spans = (got or {}).get("trace", {}).get("spans", [])
            roots = [s for s in spans if not s.get("parent_span_id") and s.get("name") == CHAIN[0]]
            if roots and int(roots[0].get("start_time_unix_nano", 0)) >= int((asked - 5) * 1e9):
                trace = got
                break
        if trace is None:
            time.sleep(2)
    if not rep(trace is not None, f"credit: a `{CHAIN[0]}` trace within {NFR_OBS_1_S} s (NFR-OBS-1)"):
        return
    spans = trace["trace"]["spans"]
    by_id = {s["span_id"]: s for s in spans}
    info({"span_names": sorted({s["name"] for s in spans})[:20]})
    for parent, child in zip(CHAIN, CHAIN[1:], strict=False):
        kids = [s for s in spans if s["name"] == child]
        ok = bool(kids) and all(by_id.get(s.get("parent_span_id"), {}).get("name") == parent for s in kids)
        rep(ok, f"credit: {child} is a child of {parent}")
    # FR-OBS-6 (c): the CLI's spans under the real account stay within VEC-OBS-2 (names, attribute and resource keys).
    vec = YAML(typ="safe").load(VEC_OBS_2.read_text(encoding="utf-8"))
    cli = [s for s in spans if s["name"].startswith("claude_code.")]
    names = {s["name"] for s in cli}
    rep(names <= set(vec["spans"]), "credit: the CLI's span names are in VEC-OBS-2", ",".join(sorted(names - set(vec["spans"]))))
    extra: set[str] = set()
    for s in cli:
        allowed = set(vec["spans"].get(s["name"], {}).get("keys", []))
        extra |= {k for k in _keys(s.get("attributes")) if not k.startswith("mlflow.")} - allowed
    rep(not extra, "credit: the CLI's span attribute keys are in VEC-OBS-2", ",".join(sorted(extra)[:10]))
    tags = _keys((trace["trace"].get("trace_info") or {}).get("tags") or {})
    resource = {k for k in tags if not k.startswith("mlflow.")}
    unknown = resource - set(vec["resource_keys"]) - OURS_RESOURCE
    # MLflow keeps resource attributes as trace tags; the CLI's and FR-OBS-1's resource keys are the only ones allowed.
    rep(not unknown, "credit: the trace's resource keys are VEC-OBS-2's or FR-OBS-1's", ",".join(sorted(unknown)))
    rep(canary not in json.dumps(trace), "credit: the canary is absent from the trace JSON")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="reader")
    ap.add_argument("--out", required=True)
    ap.add_argument("--with-credit", action="store_true")
    ap.add_argument("--browser", default=None, help="the Playwright script (obs_browser.mjs)")
    a = ap.parse_args()
    out = Path(a.out)
    root = out / "caddy-root.crt"
    spki = web_live.fetch_root(a.project, root)
    if not rep(bool(spki), "Caddy's public root read (as `reader trust` reads it)"):
        print(f"fails={fails}")
        return 1
    owner, learner = _token(), _token()
    o_ins, o_del = session_sql(owner)
    l_ins, l_del = learner_sql(learner)
    created = [rep(one_row(a.project, o_ins), "owner session row created (one row, by id)")]
    created.append(rep(one_row(a.project, l_ins), "learner session row created (one row, by id)"))
    try:
        if all(created):
            with httpx.Client(verify=str(root), timeout=30, follow_redirects=False) as c:
                gate_checks(c, owner, learner)
                ids = summary_checks(c, owner, learner)
                target = dashboard_target(c, owner)
                (out / "summary-trace-ids.json").write_text(json.dumps(ids))
                (out / "dashboard-trace.json").write_text(json.dumps(target))
                if a.with_credit:
                    credit_checks(c, owner)
            if a.browser:
                env = {
                    "PATH": web_live.node_path(), "HOME": os.environ.get("HOME", ""),
                    "READER_URL": APP, "READER_CA_SPKI": spki or "",
                    "NODE_EXTRA_CA_CERTS": str(root), "SHOTS": str(out), "READER_SESSION": owner,
                }
                rc = subprocess.run(["node", a.browser], env=env, timeout=900, check=False).returncode
                rep(rc == 0, "browser checks exited cleanly", str(rc))
    finally:
        rep(one_row(a.project, o_del), "owner session row deleted (one row, by id)")
        rep(one_row(a.project, l_del), "learner session row deleted (one row, by id)")
    print(f"fails={fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
