# @trace INV-SANDBOX-1, INV-SANDBOX-2, INV-SANDBOX-3, INV-SANDBOX-4
# @trace INV-SANDBOX-5, INV-SANDBOX-6, VEC-SANDBOX-1
"""OPS-SANDBOX invariants at the [image] level: no egress, no secret paths, other names unresolvable and
the caller's ports closed (the full [live] form runs in e2e/live/slice.sh), limits, reset leaves nothing, and
kernel code cannot forge a reset. Probe targets come from VEC-SANDBOX-1."""

import json
import time

from .conftest import PROBES, Sandbox, load_yaml, requires_docker_daemon

RUN_PROBE = r'''
import json, os, socket, sys, urllib.request
action, expect = sys.argv[1], sys.argv[2]
try:
    value = eval(action)
    outcome = "absent" if value is False else "ok"
except socket.gaierror:
    outcome = "resolve_fails"
except OSError:
    outcome = "connect_fails"
print(json.dumps(outcome))
'''


@requires_docker_daemon
def test_image_level_probes(sandbox: Sandbox) -> None:
    sandbox.start()
    probes = [p for p in load_yaml(PROBES)["probes"] if "image" in p["level"] and p["expect"] != "equals"]
    required = {"dns-external", "ip-external", "http-external", "secrets-dir", "docker-sock"}
    assert {p["id"] for p in probes} >= required
    for p in probes:
        code = RUN_PROBE.replace("sys.argv[1], sys.argv[2]", f"{p['action']!r}, {p['expect']!r}")
        out = sandbox.exec_ok(code, timeout=60)
        outcome = json.loads(out)
        allowed = {p["expect"]} | ({"resolve_fails"} if p["expect"] == "connect_fails" else set())
        assert outcome in allowed, (p["id"], outcome)


@requires_docker_daemon
def test_other_names_unresolvable_and_caller_ports_closed(sandbox: Sandbox) -> None:
    sandbox.start()
    sandbox.sidecar("api")
    out = json.loads(sandbox.exec_ok(r'''
import json, socket
names = {}
for n in ("postgres", "worker", "caddy", "mcp", "agent-proxy", "kernel-verify", "kernel-agent",
          "kernel-pair-1", "kernel-user-someone-else"):
    try:
        socket.getaddrinfo(n, 80)
        names[n] = "resolves"
    except socket.gaierror:
        names[n] = "unresolvable"
addrs = sorted({ai[4][0] for ai in socket.getaddrinfo("api", 80, socket.AF_INET)})
open_ports = []
for a in addrs:
    for port in (22, 80, 443, 2019, 5432, 8000, 8001, 8888, 8889):
        s = socket.socket()
        s.settimeout(1)
        if s.connect_ex((a, port)) == 0:
            open_ports.append((a, port))
        s.close()
print(json.dumps({"names": names, "api": addrs, "open": open_ports}))
''', timeout=120))
    assert set(out["names"].values()) == {"unresolvable"}, out["names"]
    assert out["api"], "the stand-in caller must resolve"
    assert out["open"] == []


@requires_docker_daemon
def test_limits_caps_oom_and_pids(sandbox: Sandbox) -> None:
    nonce = sandbox.start()["nonce"]
    status = json.loads(sandbox.exec_ok(r'''
import json
fields = dict(line.split(":", 1) for line in open("/proc/self/status") if ":" in line)
print(json.dumps({k: fields[k].split()[0] for k in ("Uid", "CapEff", "NoNewPrivs", "Seccomp")}))
'''))
    assert status == {"Uid": "1000", "CapEff": "0000000000000000", "NoNewPrivs": "1", "Seccomp": "2"}
    res = sandbox.cell("x = bytearray(2_600_000_000)\nprint('allocated')", wait_s=60)
    assert "allocated" not in res["stdout"]
    assert "restarting" in res["states"] or "dead" in res["states"] or res.get("closed"), res
    assert sandbox.state()["nonce"] == nonce
    out = json.loads(sandbox.exec_ok(r'''
import json, subprocess
procs, error = [], None
for _ in range(400):
    try:
        procs.append(subprocess.Popen(["sleep", "30"]))
    except OSError as e:
        error = type(e).__name__
        break
import urllib.request
try:
    with urllib.request.urlopen("http://127.0.0.1:8889/control/state", timeout=3) as r:
        during = json.loads(r.read())
except OSError as e:
    during = {"error": type(e).__name__}
for p in procs:
    p.kill()
    p.wait()
print(json.dumps({"started": len(procs), "error": error, "during": during}))
''', timeout=120))
    assert out["error"] is not None, out
    assert out["started"] < 256
    assert out["during"].get("nonce") == nonce, out["during"]
    time.sleep(1)
    state = sandbox.state()
    assert state.get("nonce") == nonce
    assert state.get("gateway") == "up"


@requires_docker_daemon
def test_reset_leaves_nothing_behind(sandbox: Sandbox) -> None:
    nonce = sandbox.start()["nonce"]
    planted = json.loads(sandbox.exec_ok(r'''
import json, os, subprocess, sys
paths = ["/home/lab/m", "/tmp/m", "/run/evcxr/m", "/dev/shm/m", "/dev/mqueue/m"]
made = {}
for p in paths:
    try:
        fd = os.open(p, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        made[p] = True
    except OSError as e:
        made[p] = type(e).__name__
subprocess.Popen([sys.executable, "-c",
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)  # survivor-marker"],
    start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(json.dumps(made))
'''))
    assert all(v is True for v in planted.values()), planted
    sandbox.exec(r'''
import urllib.request
urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8889/control/exit", data=b"", method="POST"),
                       timeout=5)
''')
    sandbox.wait_state(lambda s: s.get("nonce") not in (None, nonce) and s.get("gateway") == "up")
    left = json.loads(sandbox.exec_ok(r'''
import json, os
paths = ["/home/lab/m", "/tmp/m", "/run/evcxr/m", "/dev/shm/m", "/dev/mqueue/m"]
alive = []
for pid in os.listdir("/proc"):
    if pid.isdigit() and pid != str(os.getpid()):
        try:
            if b"survivor-marker" in open(f"/proc/{pid}/cmdline", "rb").read():
                alive.append(pid)
        except OSError:
            pass
print(json.dumps({"files": [p for p in paths if os.path.exists(p)], "alive": alive}))
'''))
    assert left == {"files": [], "alive": []}


@requires_docker_daemon
def test_kernel_code_cannot_forge(sandbox: Sandbox) -> None:
    nonce = sandbox.start()["nonce"]
    out = json.loads(sandbox.exec_ok(r'''
import ctypes, errno, json, socket
libc = ctypes.CDLL(None, use_errno=True)
res = {}
libc.ptrace.restype = ctypes.c_long
r = libc.ptrace(16, 1, None, None)  # PTRACE_ATTACH
res["ptrace"] = errno.errorcode.get(ctypes.get_errno(), str(ctypes.get_errno())) if r == -1 else "attached"
class iovec(ctypes.Structure):
    _fields_ = [("base", ctypes.c_void_p), ("len", ctypes.c_size_t)]
buf = ctypes.create_string_buffer(8)
local = iovec(ctypes.cast(buf, ctypes.c_void_p), 8)
remote = iovec(ctypes.c_void_p(0x10000), 8)
libc.process_vm_readv.restype = ctypes.c_ssize_t
ctypes.set_errno(0)
r = libc.process_vm_readv(1, ctypes.byref(local), 1, ctypes.byref(remote), 1, 0)
res["vm_readv"] = errno.errorcode.get(ctypes.get_errno(), "?") if r == -1 else "read"
for mode in ("rb", "r+b"):
    try:
        open("/proc/1/mem", mode)
        res["mem_" + mode] = "opened"
    except OSError as e:
        res["mem_" + mode] = type(e).__name__
for fam, addr in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
    try:
        s = socket.socket(fam, socket.SOCK_STREAM)
        s.bind((addr, 8889))
        res["bind_" + addr] = "bound"
    except OSError as e:
        res["bind_" + addr] = errno.errorcode.get(e.errno, str(e.errno))
print(json.dumps(res))
'''))
    assert out["ptrace"] == "EPERM"
    assert out["vm_readv"] == "EPERM"
    assert out["mem_rb"] == "PermissionError"
    assert out["mem_r+b"] == "PermissionError"
    assert out["bind_0.0.0.0"] == "EADDRINUSE"
    assert out["bind_::"] in {"EADDRINUSE", "EADDRNOTAVAIL", "EAFNOSUPPORT"}
    sig = json.loads(sandbox.exec_ok(r'''
import json, os, signal, time, urllib.request
open("/tmp/marker2", "w").write("m")
for s in (signal.SIGCHLD, signal.SIGPIPE):
    os.kill(1, s)
    time.sleep(0.2)
with urllib.request.urlopen("http://127.0.0.1:8889/control/state", timeout=2) as r:
    print(json.dumps({"nonce": json.loads(r.read())["nonce"], "marker": os.path.exists("/tmp/marker2")}))
'''))
    assert sig == {"nonce": nonce, "marker": True}
