"""Runs INSIDE the converter's sandbox: SRV-INGEST INV-INGEST-4's cross-process probes (spikes/ingest-sysctl).

@trace INV-INGEST-4

Argument: JSON {target, work_b, work_b_dev, work_b_ino}. Prints one JSON line with three groups:
- "other": each channel against the other process → "allowed:…" or "denied:errno N" (the test needs errno 1);
- "self": the same kinds of query against this process, which the profile's `(target self)` must allow;
- "recorded": channels the profile cannot close, recorded for the reader (own argv, fsgetpath, chdir).
"""

import ctypes
import json
import os
import sys

libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
args = json.loads(sys.argv[1])
target = int(args["target"])
me = os.getpid()
other: dict[str, str] = {}
mine: dict[str, str] = {}
recorded: dict[str, str] = {}


def sysctl(mib: list[int]) -> str:
    """A size query first (no buffer), so a large table cannot fail with ENOMEM instead of EPERM."""
    arr = (ctypes.c_int * len(mib))(*mib)
    size = ctypes.c_size_t(0)
    if libc.sysctl(arr, len(mib), None, ctypes.byref(size), None, 0) != 0:
        return f"denied:errno {ctypes.get_errno()}"
    size.value = size.value * 2 + 4096
    buf = ctypes.create_string_buffer(size.value)
    if libc.sysctl(arr, len(mib), buf, ctypes.byref(size), None, 0) != 0:
        return f"denied:errno {ctypes.get_errno()}"
    return f"allowed:{size.value}"


def libproc(call: str, pid: int) -> str:
    buf = ctypes.create_string_buffer(8192)
    if call == "proc_pidpath":
        n = libc.proc_pidpath(pid, buf, 4096)
    elif call == "proc_name":
        n = libc.proc_name(pid, buf, 1024)
    elif call == "proc_pid_rusage":
        n = 1 if libc.proc_pid_rusage(pid, 0, buf) == 0 else 0  # RUSAGE_INFO_V0
    elif call == "proc_pidfdinfo":
        n = libc.proc_pidfdinfo(pid, 0, 2, buf, 8192)  # fd 0, PROC_PIDFDVNODEPATHINFO
    else:
        flavor = {"PROC_PIDLISTFDS": 1, "PROC_PIDTBSDINFO": 3, "PROC_PIDVNODEPATHINFO": 9}[call]
        n = libc.proc_pidinfo(pid, flavor, 0, buf, 8192)
    return "allowed" if n > 0 else f"denied:errno {ctypes.get_errno()}"


KERN, PROCARGS, PROCARGS2, PROC = 1, 38, 49, 14
other["kern.procargs2"] = sysctl([KERN, PROCARGS2, target])
other["kern.procargs"] = sysctl([KERN, PROCARGS, target])
other["kern.proc.pid"] = sysctl([KERN, PROC, 1, target])
other["kern.proc.pgrp"] = sysctl([KERN, PROC, 2, target])
other["kern.proc.tty"] = sysctl([KERN, PROC, 4, 0])
other["kern.proc.uid"] = sysctl([KERN, PROC, 5, os.getuid()])
other["kern.proc.ruid"] = sysctl([KERN, PROC, 6, os.getuid()])
other["kern.proc.all"] = sysctl([KERN, PROC, 0])
for call in (
    "proc_pidpath",
    "proc_name",
    "proc_pid_rusage",
    "proc_pidfdinfo",
    "PROC_PIDLISTFDS",
    "PROC_PIDTBSDINFO",
    "PROC_PIDVNODEPATHINFO",
):
    other[call] = libproc(call, target)
pids = (ctypes.c_int * 8192)()
n = libc.proc_listallpids(pids, ctypes.sizeof(pids))
other["proc_listallpids"] = f"allowed:{n}" if n > 0 else f"denied:errno {ctypes.get_errno()}"
try:
    os.listdir(os.path.dirname(args["work_b"]))
    other["list WORK parent"] = "allowed"
except OSError as e:
    other["list WORK parent"] = f"denied:errno {e.errno}"

# Positive controls: the same kinds of query against this process.
mine["kern.procargs2"] = sysctl([KERN, PROCARGS2, me])
mine["kern.proc.pid"] = sysctl([KERN, PROC, 1, me])
for call in ("proc_pidpath", "PROC_PIDVNODEPATHINFO"):
    mine[call] = libproc(call, me)


class Fsid(ctypes.Structure):
    _fields_ = [("val", ctypes.c_int32 * 2)]


fsid = Fsid()
fsid.val[0] = int(args["work_b_dev"])
pbuf = ctypes.create_string_buffer(1024)
n = libc.fsgetpath(pbuf, 1024, ctypes.byref(fsid), ctypes.c_uint64(int(args["work_b_ino"])))
recorded["fsgetpath of the other WORK"] = "allowed" if n > 0 else f"denied:errno {ctypes.get_errno()}"
try:
    os.chdir(args["work_b"])
    recorded["chdir into the other WORK (path known)"] = "allowed"
    try:
        os.listdir(".")
        recorded["list the other WORK after chdir"] = "allowed"
    except OSError as e:
        recorded["list the other WORK after chdir"] = f"denied:errno {e.errno}"
except OSError as e:
    recorded["chdir into the other WORK (path known)"] = f"denied:errno {e.errno}"
recorded["own kern.procargs2"] = mine["kern.procargs2"]
print(json.dumps({"other": other, "self": mine, "recorded": recorded}), flush=True)
