"""Runs INSIDE the converter's sandbox: SRV-INGEST INV-INGEST-4's cross-process probes (spikes/ingest-sysctl).

@trace INV-INGEST-4

Argument: JSON {target, work_b, work_b_dev, work_b_ino}. Prints one JSON line, channel → "allowed…" or
"denied:errno N". The caller asserts every process channel is denied; inode channels are recorded only.
"""

import ctypes
import json
import os
import sys

libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
args = json.loads(sys.argv[1])
target = int(args["target"])
out: dict[str, str] = {}


def sysctl(name: str, mib: list[int]) -> None:
    arr = (ctypes.c_int * len(mib))(*mib)
    size = ctypes.c_size_t(1 << 20)
    buf = ctypes.create_string_buffer(1 << 20)
    ok = libc.sysctl(arr, len(mib), buf, ctypes.byref(size), None, 0) == 0
    out[name] = f"allowed:{size.value}" if ok else f"denied:errno {ctypes.get_errno()}"


sysctl("kern.procargs2", [1, 49, target])
sysctl("kern.procargs", [1, 38, target])
sysctl("kern.proc.pid", [1, 14, 1, target])
sysctl("kern.proc.all", [1, 14, 0])

buf = ctypes.create_string_buffer(4096)
n = libc.proc_pidpath(target, buf, 4096)
out["proc_pidpath"] = "allowed" if n > 0 else f"denied:errno {ctypes.get_errno()}"
vbuf = ctypes.create_string_buffer(2352)
n = libc.proc_pidinfo(target, 9, 0, vbuf, 2352)  # PROC_PIDVNODEPATHINFO
out["proc_pidinfo cwd"] = "allowed" if n > 0 else f"denied:errno {ctypes.get_errno()}"
pids = (ctypes.c_int * 8192)()
n = libc.proc_listallpids(pids, ctypes.sizeof(pids))
out["proc_listallpids"] = "allowed" if n > 0 else f"denied:errno {ctypes.get_errno()}"
try:
    os.listdir(os.path.dirname(args["work_b"]))
    out["list WORK parent"] = "allowed"
except OSError as e:
    out["list WORK parent"] = f"denied:errno {e.errno}"


class Fsid(ctypes.Structure):
    _fields_ = [("val", ctypes.c_int32 * 2)]


fsid = Fsid()
fsid.val[0] = int(args["work_b_dev"])
pbuf = ctypes.create_string_buffer(1024)
n = libc.fsgetpath(pbuf, 1024, ctypes.byref(fsid), ctypes.c_uint64(int(args["work_b_ino"])))
out["recorded fsgetpath"] = "allowed" if n > 0 else f"denied:errno {ctypes.get_errno()}"
print(json.dumps(out), flush=True)
