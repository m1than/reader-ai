"""Spike ingest-confine: negative and positive probes run inside the converter profile.

    <venv>/bin/python -I probes.py <work-dir> <outside-path-to-try-writing>

Prints one JSON object: probe name -> "allowed" or the error. Probes touch no secret: the Keychain probe only
looks up a Mach service name and never asks for an item; the docker probe connects and closes.
"""

import ctypes
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

work, outside = Path(sys.argv[1]), Path(sys.argv[2])
home = Path("/Users") / os.environ.get("PROBE_USER", "m1than")
results: dict[str, str] = {}


def probe(name: str, fn) -> None:  # type: ignore[no-untyped-def]
    try:
        fn()
        results[name] = "allowed"
    except Exception as exc:  # noqa: BLE001 - the error class and errno are the measurement
        results[name] = f"{type(exc).__name__}: {getattr(exc, 'errno', '')} {getattr(exc, 'strerror', '') or ''}".strip()


def tcp_connect() -> None:
    with socket.create_connection(("127.0.0.1", 9), timeout=2):
        pass


def tcp_bind() -> None:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    s.close()


def unix_connect(path: str):  # type: ignore[no-untyped-def]
    def run() -> None:
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(2)
        s.connect(path)
        s.close()
    return run


def unix_bind() -> None:
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(work / "probe.sock"))
    s.close()


def mach_lookup(service: str):  # type: ignore[no-untyped-def]
    def run() -> None:
        lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        port = ctypes.c_uint32()
        bootstrap = ctypes.c_uint32.in_dll(lib, "bootstrap_port")
        rc = lib.bootstrap_look_up(bootstrap, service.encode(), ctypes.byref(port))
        if rc != 0:
            raise PermissionError(rc, f"bootstrap_look_up kern_return {rc}")
    return run


def exec_(path: str):  # type: ignore[no-untyped-def]
    def run() -> None:
        subprocess.run([path, "--version"], capture_output=True, timeout=5, check=False)
    return run


def posix_semaphore() -> None:
    import multiprocessing
    multiprocessing.Lock()


def posix_shm() -> None:
    from multiprocessing import shared_memory
    shared_memory.SharedMemory(create=True, size=4096).unlink()


probe("tcp_connect_127.0.0.1", tcp_connect)
probe("tcp_bind_listen_127.0.0.1", tcp_bind)
probe("unix_connect_docker_user_socket", unix_connect(str(home / ".docker/run/docker.sock")))
probe("unix_connect_var_run_docker_sock", unix_connect("/var/run/docker.sock"))
probe("unix_bind_in_work", unix_bind)
probe("socketpair", lambda: socket.socketpair())
probe("mach_lookup_SecurityServer", mach_lookup("com.apple.SecurityServer"))
probe("mach_lookup_launchservicesd", mach_lookup("com.apple.coreservices.launchservicesd"))
probe("exec_usr_bin_security", exec_("/usr/bin/security"))
probe("exec_docker", exec_("/usr/local/bin/docker"))
probe("exec_bin_sh", exec_("/bin/sh"))
probe("signal_parent", lambda: os.kill(os.getppid(), 0))
probe("open_dev_tty", lambda: open("/dev/tty").close())
probe("list_home", lambda: os.listdir(home))
probe("read_repo_file_in_home", lambda: (home / "Developer/reader-ai/AGENTS.md").read_bytes())
probe("read_etc_passwd", lambda: Path("/etc/passwd").read_bytes())
probe("write_outside_work", lambda: outside.write_text("x"))
probe("write_in_work", lambda: (work / "probe.txt").write_text("x"))
probe("symlink_in_work_to_outside", lambda: os.symlink("/etc/hosts", work / "probe-link"))
probe("mkfifo_in_work", lambda: os.mkfifo(work / "probe-fifo"))
probe("posix_semaphore", posix_semaphore)
probe("posix_shm", posix_shm)
print(json.dumps(results, indent=1))
