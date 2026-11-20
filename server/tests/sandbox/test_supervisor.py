# @trace FR-SANDBOX-4, FR-SANDBOX-5, FR-SANDBOX-6, FR-SANDBOX-7, FR-SANDBOX-9
"""OPS-SANDBOX supervisor: isolated Python, start nonce held in memory, control endpoint, fail-stop exits,
signal dispositions, orphan reaping, the health command and the reset round trip. Exits are triggered from
inside the container (`docker exec` as uid 1000), never by `docker stop` or `docker kill`."""

import json
import time
import uuid
from datetime import datetime

from .conftest import (
    IMAGE,
    LABEL,
    NONCE,
    RESET_DEADLINE_S,
    SANDBOX,
    Build,
    Sandbox,
    docker,
    requires_docker_daemon,
)

SHEBANG = "#!/usr/local/bin/python3 -I"

POST_EXIT = r'''
import json, sys, time, urllib.request
req = urllib.request.Request("http://127.0.0.1:8889/control/exit", data=b"", method="POST")
with urllib.request.urlopen(req, timeout=5) as r:
    status = r.status
t = time.time()
try:
    with urllib.request.urlopen("http://127.0.0.1:8889/control/state", timeout=1) as r:
        after = json.loads(r.read())
except Exception as e:
    after = {"error": type(e).__name__}
print(json.dumps({"status": status, "t": t, "after": after}), flush=True)
time.sleep(30)
'''


def _ts(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _post_exit(sandbox: Sandbox) -> dict[str, object]:
    proc = sandbox.exec(POST_EXIT, timeout=60)
    line = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else "{}"
    result: dict[str, object] = json.loads(line)
    return result


def test_entrypoints_run_isolated_python() -> None:
    entry = SANDBOX / "reader_kernel_entry.py"
    health = SANDBOX / "reader_kernel_health.py"
    assert entry.exists(), "sandbox/reader_kernel_entry.py does not exist"
    assert health.exists(), "sandbox/reader_kernel_health.py does not exist"
    assert entry.read_text(encoding="utf-8").splitlines()[0] == SHEBANG
    assert health.read_text(encoding="utf-8").splitlines()[0] == SHEBANG
    assert "PR_SET_DUMPABLE" in entry.read_text(encoding="utf-8")


@requires_docker_daemon
def test_pid1_is_supervisor_nonce_in_memory_only(sandbox: Sandbox) -> None:
    state = sandbox.start()
    img = json.loads(docker("image", "inspect", IMAGE).stdout)[0]
    assert img["Config"]["Entrypoint"] == ["/usr/local/bin/reader-kernel-entry"]
    assert NONCE.match(str(state["nonce"]))
    found = sandbox.exec_ok(f"nonce = {state['nonce']!r}\n" + r'''
import os
hits = []
for top in ("/tmp", "/home/lab", "/run", "/dev/shm", "/run/evcxr"):
    for root, _, files in os.walk(top):
        for n in files:
            p = os.path.join(root, n)
            try:
                if nonce.encode() in open(p, "rb").read():
                    hits.append(p)
            except OSError:
                pass
try:
    open("/proc/1/environ", "rb").read()
    hits.append("environ-readable")
except OSError:
    pass
print(hits)
''')
    assert found.strip() == "[]"


@requires_docker_daemon
def test_control_endpoint_from_caller(sandbox: Sandbox) -> None:
    sandbox.start()
    resp = sandbox.http("GET", 8889, "/control/state")
    assert resp["status"] == 200
    body = json.loads(resp["body"])
    assert set(body) == {"nonce", "gateway"}
    assert body["gateway"] == "up"
    assert NONCE.match(body["nonce"])
    assert sandbox.http("GET", 8889, "/control/other")["status"] == 404
    assert sandbox.http("PUT", 8889, "/control/exit", "{}")["status"] == 404
    assert sandbox.http("GET", 8889, "/control/exit")["status"] == 404
    assert sandbox.http("POST", 8889, "/control/exit", "x" * 2048)["status"] == 413
    assert sandbox.state()["nonce"] == body["nonce"]


@requires_docker_daemon
def test_exit_reports_exiting_and_restarts_with_new_nonce_three_times(sandbox: Sandbox) -> None:
    nonce = sandbox.start()["nonce"]
    durations = []
    for _ in range(3):
        start = time.monotonic()
        result = _post_exit(sandbox)
        assert result.get("status") == 202, result
        after = result.get("after")
        assert isinstance(after, dict)
        assert after.get("gateway") == "exiting" or "error" in after, after
        old = nonce
        state = sandbox.wait_state(
            lambda s, old=old: s.get("nonce") not in (None, old) and s.get("gateway") == "up",
            timeout=RESET_DEADLINE_S,
        )
        durations.append(time.monotonic() - start)
        nonce = state["nonce"]
        time.sleep(11)  # let Docker's restart delay reset (it doubles for containers that ran < 10 s)
    print(f"reset round trips: {[round(d, 2) for d in durations]} s")
    assert all(d < RESET_DEADLINE_S for d in durations)


@requires_docker_daemon
def test_exit_is_unconditional_with_idle_connections(sandbox: Sandbox) -> None:
    sandbox.start()
    before = sandbox.inspect()["State"]["StartedAt"]
    proc = sandbox.exec(r'''
import json, socket, time, urllib.request
held = [socket.create_connection(("127.0.0.1", 8889)) for _ in range(50)]
req = urllib.request.Request("http://127.0.0.1:8889/control/exit", data=b"", method="POST")
with urllib.request.urlopen(req, timeout=5) as r:
    status = r.status
print(json.dumps({"status": status, "t": time.time()}), flush=True)
time.sleep(30)
''', timeout=60)
    result = json.loads(proc.stdout.strip().splitlines()[0])
    assert result["status"] == 202
    sandbox.wait_state(lambda s: s.get("gateway") == "up" and "nonce" in s, timeout=RESET_DEADLINE_S)
    info = sandbox.inspect()
    assert info["State"]["StartedAt"] != before
    gone_after = _ts(info["State"]["FinishedAt"]) - float(result["t"])
    print(f"process gone {gone_after:.2f} s after the 202, with 50 idle connections")
    assert gone_after <= 3.0


def _die_codes(sandbox: Sandbox, since: int) -> list[str]:
    proc = docker("events", "--since", str(since), "--until", str(int(time.time()) + 1),
                  "--filter", f"container={sandbox.name}", "--filter", "event=die",
                  "--format", "{{.Actor.Attributes.exitCode}}", timeout=30)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.split()


@requires_docker_daemon
def test_sigterm_exits_zero_and_gateway_exit_ends_pid1(sandbox: Sandbox) -> None:
    nonce = sandbox.start()["nonce"]
    t0 = int(time.time()) - 1
    sandbox.exec("import os, signal; os.kill(1, signal.SIGTERM)")
    state = sandbox.wait_state(lambda s: s.get("nonce") not in (None, nonce) and s.get("gateway") == "up")
    assert _die_codes(sandbox, t0) == ["0"]
    nonce = state["nonce"]
    time.sleep(11)
    t1 = int(time.time()) - 1
    sandbox.exec(r'''
import os, signal
for pid in os.listdir("/proc"):
    if pid.isdigit() and pid not in ("1", str(os.getpid())):
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read()
        except OSError:
            continue
        if b"kernel_gateway" in cmd:
            os.kill(int(pid), signal.SIGKILL)
''')
    sandbox.wait_state(lambda s: s.get("nonce") not in (None, nonce) and s.get("gateway") == "up")
    assert _die_codes(sandbox, t1) == ["1"]


@requires_docker_daemon
def test_signal_dispositions(sandbox: Sandbox) -> None:
    nonce = sandbox.start()["nonce"]
    out = sandbox.exec_ok(r'''
import json, os, time, urllib.request
open("/tmp/marker", "w").write("m")
for sig in list(range(1, 65)):
    if sig in (2, 15):
        continue
    try:
        os.kill(1, sig)
    except OSError as e:
        print("kill", sig, e)
    time.sleep(0.02)
time.sleep(1)
with urllib.request.urlopen("http://127.0.0.1:8889/control/state", timeout=2) as r:
    print(json.dumps({"state": json.loads(r.read()), "marker": os.path.exists("/tmp/marker")}))
''')
    result = json.loads(out.strip().splitlines()[-1])
    assert result["state"]["nonce"] == nonce
    assert result["marker"] is True
    sandbox.exec("import os, signal; os.kill(1, signal.SIGINT)")
    state = sandbox.wait_state(lambda s: s.get("nonce") not in (None, nonce) and s.get("gateway") == "up")
    assert state["nonce"] != nonce
    assert sandbox.exec_ok("import os; print(os.path.exists('/tmp/marker'))").strip() == "False"


@requires_docker_daemon
def test_orphans_are_reaped_without_restart(sandbox: Sandbox) -> None:
    nonce = sandbox.start()["nonce"]
    sandbox.exec_ok(r'''
import os, time
open("/tmp/marker", "w").write("m")
pid = os.fork()
if pid == 0:
    if os.fork() == 0:
        time.sleep(0.5)
        os._exit(0)
    os._exit(0)
os.waitpid(pid, 0)
''')
    time.sleep(2)
    out = sandbox.exec_ok(r'''
import json, os
zombies = []
for pid in os.listdir("/proc"):
    if pid.isdigit():
        try:
            fields = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
        except OSError:
            continue
        if fields[0] == "Z":
            zombies.append(pid)
print(json.dumps({"zombies": zombies, "marker": os.path.exists("/tmp/marker")}))
''')
    result = json.loads(out)
    assert result["zombies"] == []
    assert result["marker"] is True
    assert sandbox.state()["nonce"] == nonce


@requires_docker_daemon
def test_health_command(sandbox: Sandbox) -> None:
    sandbox.start()
    proc = sandbox.run("/usr/local/bin/reader-kernel-health")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "up"


STAND_IN = r'''
import http.server, json
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"nonce": "0" * 32, "gateway": "starting"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
http.server.HTTPServer(("127.0.0.1", 8889), H).serve_forever()
'''


@requires_docker_daemon
def test_health_command_exit_one_cases(image: Build) -> None:
    assert image.ok, image.log
    proc = docker("run", "--rm", "--label", LABEL, "--network", "none", "--entrypoint",
                  "/usr/local/bin/reader-kernel-health", IMAGE)
    assert proc.returncode == 1
    assert proc.stdout.strip() == "unreachable"
    name = f"rk-standin-{uuid.uuid4().hex[:8]}"
    started = docker("run", "-d", "--name", name, "--label", LABEL, "--network", "none", "--entrypoint",
                     "python3", IMAGE, "-I", "-c", STAND_IN)
    assert started.returncode == 0, started.stderr
    try:
        time.sleep(2)
        proc = docker("exec", name, "/usr/local/bin/reader-kernel-health")
        assert proc.returncode == 1
        assert proc.stdout.strip() == "starting"
    finally:
        docker("rm", "-f", name)


FLOOD_AND_SIGNAL = r'''
import os, socket, sys, threading, time
REQ = b"GET /control/state HTTP/1.1\r\nHost: x\r\n\r\n"
def flood():
    while True:
        try:
            s = socket.create_connection(("127.0.0.1", 8889), timeout=1)
            s.sendall(REQ)
            s.recv(300)
            s.close()
        except OSError:
            pass
for _ in range(6):
    threading.Thread(target=flood, daemon=True).start()
time.sleep(0.4)
os.kill(1, int(sys.argv[1]))
time.sleep(60)
'''


@requires_docker_daemon
def test_signals_under_state_flood_always_restart(image: Build) -> None:
    """The reviewer's SIGTERM race (PID 1 hung in 4 of 40 runs) and SIGINT, on 30 fresh containers."""
    failures = []
    for i in range(30):
        sb = Sandbox(image)
        try:
            nonce = sb.start()["nonce"]
            sig = "15" if i % 2 == 0 else "2"
            docker("exec", "-d", "-u", "1000:1000", sb.name, "python3", "-I", "-c", FLOOD_AND_SIGNAL, sig)
            try:
                sb.wait_state(lambda s, n=nonce: s.get("nonce") not in (None, n), timeout=10)
            except AssertionError as e:
                failures.append((i, sig, str(e)))
        finally:
            sb.cleanup()
    assert failures == []


HOLD_TABLE = r'''
import select, socket, time
conns = []
end = time.time() + 25
while time.time() < end:
    alive = []
    for s in conns:
        r, _, _ = select.select([s], [], [], 0)
        if r:
            try:
                if s.recv(1) == b"":
                    s.close()
                    continue
            except OSError:
                s.close()
                continue
        alive.append(s)
    conns = alive
    for _ in range(400 - len(conns)):
        try:
            conns.append(socket.create_connection(("127.0.0.1", 8889), timeout=0.2))
        except OSError:
            break
    time.sleep(0.05)
'''


@requires_docker_daemon
def test_full_table_from_inside_does_not_block_the_caller(sandbox: Sandbox) -> None:
    nonce = sandbox.start()["nonce"]
    docker("exec", "-d", "-u", "1000:1000", sandbox.name, "python3", "-I", "-c", HOLD_TABLE)
    time.sleep(3)
    t0 = time.monotonic()
    state = sandbox.http("GET", 8889, "/control/state")
    assert state["status"] == 200
    assert json.loads(state["body"])["nonce"] == nonce
    exit_resp = sandbox.http("POST", 8889, "/control/exit")
    assert exit_resp["status"] == 202
    assert time.monotonic() - t0 <= 10.0
    sandbox.wait_state(lambda s: s.get("nonce") not in (None, nonce) and s.get("gateway") == "up")


@requires_docker_daemon
def test_idle_connection_closed_after_read_timeout(sandbox: Sandbox) -> None:
    sandbox.start()
    out = json.loads(sandbox.exec_ok(r'''
import json, socket, time
s = socket.create_connection(("127.0.0.1", 8889))
t = time.monotonic()
s.settimeout(15)
data = s.recv(10)
print(json.dumps({"after": time.monotonic() - t, "data": data.decode()}))
'''))
    assert out["data"] == ""
    assert 5.0 <= out["after"] <= 7.0, out


@requires_docker_daemon
def test_grace_kills_stopped_gateway_and_keeps_port(sandbox: Sandbox) -> None:
    sandbox.start()
    proc = sandbox.exec(r'''
import errno, json, os, signal, socket, time, urllib.request
gw = None
for pid in os.listdir("/proc"):
    if pid.isdigit() and pid not in ("1", str(os.getpid())):
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read()
        except OSError:
            continue
        if b"jupyter-kernelgateway" in cmd:
            gw = int(pid)
os.kill(gw, signal.SIGSTOP)
req = urllib.request.Request("http://127.0.0.1:8889/control/exit", data=b"", method="POST")
with urllib.request.urlopen(req, timeout=5) as r:
    status = r.status
t = time.time()
binds = []
while time.time() - t < 1.5:
    s = socket.socket()
    try:
        s.bind(("0.0.0.0", 8889))
        binds.append("bound")
    except OSError as e:
        binds.append(errno.errorcode.get(e.errno, "?"))
    s.close()
    time.sleep(0.05)
print(json.dumps({"status": status, "t": t, "binds": sorted(set(binds))}), flush=True)
time.sleep(30)
''', timeout=60)
    result = json.loads(proc.stdout.strip().splitlines()[0])
    assert result["status"] == 202
    assert result["binds"] == ["EADDRINUSE"]
    sandbox.wait_state(lambda s: s.get("gateway") == "up" and "nonce" in s)
    gone_after = _ts(sandbox.inspect()["State"]["FinishedAt"]) - float(result["t"])
    print(f"stopped gateway: process gone {gone_after:.2f} s after the 202")
    assert 2.0 <= gone_after <= 3.0
