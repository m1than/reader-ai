"""Runs INSIDE the sandbox (spike ingest-sysctl). Argument: JSON {target, work_b_hint}.

Tries every channel the round-3 reviewer used, or could have used, to learn another run's WORK, then the
membership attack (b): fork, setsid, chdir /, rewrite argv to name the learned WORK, sleep so the driver can look.
Prints one JSON line: channel → "allowed:<detail>" or "denied:<errno>"; plus "attacker_pid" and "learned".
"""

import ctypes
import json
import os
import sys
import time

libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
args = json.loads(sys.argv[1])
target = int(args["target"])
out: dict[str, object] = {}
learned = None


def sysctl(mib: list[int]) -> tuple[bool, bytes, int]:
    arr = (ctypes.c_int * len(mib))(*mib)
    size = ctypes.c_size_t(1 << 20)
    buf = ctypes.create_string_buffer(1 << 20)
    r = libc.sysctl(arr, len(mib), buf, ctypes.byref(size), None, 0)
    return r == 0, buf.raw[: size.value], ctypes.get_errno()


def record(name: str, ok: bool, raw: bytes, err: int) -> None:
    global learned
    if ok:
        text = raw.replace(b"\0", b" ").decode(errors="replace")
        hit = args["work_b"] in text
        if hit and "self" not in name:  # the probe's own argv carries WORK_B from the driver (harness, not a leak)
            learned = args["work_b"]
        out[name] = f"allowed:{len(raw)} bytes, names WORK_B: {hit}"
    else:
        out[name] = f"denied:errno {err}"


record("kern.procargs2 other", *sysctl([1, 49, target]))
record("kern.procargs other", *sysctl([1, 38, target]))
record("kern.proc.pid other", *sysctl([1, 14, 1, target]))
record("kern.proc.all", *sysctl([1, 14, 0]))
record("kern.procargs2 self", *sysctl([1, 49, os.getpid()]))

# libproc goes through the proc_info syscall, not sysctl: the sandbox's process-info* operations.
buf = ctypes.create_string_buffer(4096)
n = libc.proc_pidpath(target, buf, 4096)
out["proc_pidpath other"] = f"allowed:{buf.value.decode(errors='replace')}" if n > 0 else f"denied:errno {ctypes.get_errno()}"
vbuf = ctypes.create_string_buffer(2352)  # struct proc_vnodepathinfo
n = libc.proc_pidinfo(target, 9, 0, vbuf, 2352)  # PROC_PIDVNODEPATHINFO: cwd and root
if n > 0:
    hit = args["work_b"].encode() in vbuf.raw
    if hit:
        learned = args["work_b"]
    out["proc_pidinfo cwd other"] = f"allowed:{n} bytes, names WORK_B: {hit}"
else:
    out["proc_pidinfo cwd other"] = f"denied:errno {ctypes.get_errno()}"
pids = (ctypes.c_int * 8192)()
n = libc.proc_listallpids(pids, ctypes.sizeof(pids))
out["proc_listallpids"] = f"allowed:{n} pids" if n > 0 else f"denied:errno {ctypes.get_errno()}"
try:
    os.listdir(os.path.dirname(args["work_b"]))
    out["list WORK parent"] = "allowed"
except OSError as e:
    out["list WORK parent"] = f"denied:errno {e.errno}"
try:
    os.chdir(args["work_b"])
    out["chdir WORK_B"] = "allowed"
except OSError as e:
    out["chdir WORK_B"] = f"denied:errno {e.errno}"
out["learned"] = learned is not None

# Attack (b): the argv names what was learned; without a leak, the best the child can do is its own WORK's parent
# plus a guess, which cannot match a random 16-hex name.
try:
    os.getcwd()
    out["getcwd self"] = "allowed"
except OSError as e:
    out["getcwd self"] = f"denied:errno {e.errno}"  # torch's import fails without it (SRV-INGEST FR-INGEST-4)
claim = learned or os.path.join(os.path.dirname(os.environ["HOME"].rsplit("/", 1)[0]), "0" * 16)
pid = os.fork()
if pid == 0:
    os.setsid()
    os.chdir(learned or "/")  # a learned WORK also serves as cwd when chdir into it is allowed
    null = os.open("/dev/null", os.O_WRONLY)
    os.dup2(null, 1)
    os.dup2(null, 2)
    libc._NSGetArgv.restype = ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
    libc._NSGetArgc.restype = ctypes.POINTER(ctypes.c_int)
    argv = libc._NSGetArgv().contents
    argc = libc._NSGetArgc().contents.value
    start, last = argv[0], argv[argc - 1]
    end = last + len(ctypes.string_at(last))
    new = b"x\0--input\0" + claim.encode() + b"/in/material.pdf\0"
    room = end - start
    ctypes.memmove(start, (new + b"\0" * room)[:room], room)
    time.sleep(8)
    os._exit(0)
out["attacker_pid"] = pid
print(json.dumps(out), flush=True)
time.sleep(1)
