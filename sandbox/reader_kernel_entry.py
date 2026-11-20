#!/usr/local/bin/python3 -I
"""Supervisor, PID 1 of reader-kernel:local (spec/modules/OPS-SANDBOX.yaml FR-SANDBOX-4..9).

It holds a start nonce in memory, starts Kernel Gateway as its child and serves the control endpoint on port 8889:
`GET /control/state` and `POST /control/exit`. It is fail-stop: a gateway exit, an uncaught exception, SIGINT or the
end of its control loop ends PID 1, which ends every process in the container; `restart: always` then brings a fresh
container with a new nonce (ADR-0022 reset without a Docker socket). Standard library only.

Concurrency rules (FR-SANDBOX-5): no lock exists. Each piece of state is a single attribute written by exactly one
thread (`exiting` by the main thread, `gateway_seen` by the monitor) and read by others. Signal handlers never touch
state: SIGTERM writes one byte to a self-pipe that the control loop watches, and the loop runs the exit path in normal
context; SIGINT calls `os._exit(1)`.
"""

import ctypes
import errno
import ipaddress
import json
import os
import selectors
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

GATEWAY = "/usr/local/bin/jupyter-kernelgateway"
GATEWAY_PORT = 8888
CONTROL_PORT = 8889
GRACE_S = 2.0           # FR-SANDBOX-6 (b)
READ_TIMEOUT_S = 5.0    # FR-SANDBOX-6 (c)
MAX_BODY = 1024         # FR-SANDBOX-6 (c)
MAX_HEADER = 8192
LOCAL_SLOTS = 192       # FR-SANDBOX-6 (d): connections from inside the container
REMOTE_SLOTS = 64       # FR-SANDBOX-6 (d): reserved for the caller; the oldest is evicted when full
PR_SET_DUMPABLE = 4
NO_IPV6 = {errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL, errno.EPROTONOSUPPORT}
REASONS = {200: "OK", 202: "Accepted", 404: "Not Found", 413: "Content Too Large"}


def die(code: int) -> None:
    os._exit(code)


def is_local(conn: socket.socket, peer: str) -> bool:
    """A peer is local when it is a loopback address or one of the container's own addresses."""
    ip = ipaddress.ip_address(peer.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return True
    try:
        own = str(conn.getsockname()[0]).split("%", 1)[0]
    except OSError:
        return True
    return peer.split("%", 1)[0] == own or str(ip) == own


class Supervisor:
    def __init__(self) -> None:
        self.nonce = os.urandom(16).hex()
        self.exiting = False          # written only by the main thread
        self.gateway_seen = False     # written only by the monitor thread
        self.exit_armed = threading.Event()
        self.gateway: subprocess.Popen[bytes] | None = None
        self.wake_r, self.wake_w = os.pipe()
        os.set_blocking(self.wake_r, False)
        os.set_blocking(self.wake_w, False)

    @property
    def state(self) -> str:
        if self.exiting:
            return "exiting"
        return "up" if self.gateway_seen else "starting"

    # -- signals (FR-SANDBOX-5) ------------------------------------------------------------------------------
    def on_sigterm(self, *_: object) -> None:
        """Signal context: only wake the control loop."""
        try:
            os.write(self.wake_w, b"T")
        except OSError:
            pass

    # -- exit path (FR-SANDBOX-6 b), main thread only ----------------------------------------------------------
    def begin_exit(self) -> None:
        self.exiting = True
        if self.gateway is None:
            die(0)
        if not self.exit_armed.is_set():
            try:
                os.killpg(self.gateway.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            self.exit_armed.set()

    def watchdog(self) -> None:
        """Unconditional exit GRACE_S after an exit is accepted; independent of the control loop."""
        self.exit_armed.wait()
        time.sleep(GRACE_S)
        if self.gateway is not None:
            try:
                os.killpg(self.gateway.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        die(0)

    # -- children (FR-SANDBOX-5) -----------------------------------------------------------------------------
    def reaper(self) -> None:
        """Reap every child, orphans included; only the gateway's exit ends PID 1. Takes no lock."""
        while True:
            try:
                pid, _ = os.waitpid(-1, 0)
            except ChildProcessError:
                time.sleep(0.05)
                continue
            except InterruptedError:
                continue
            if self.gateway is not None and pid == self.gateway.pid:
                die(0 if self.exiting else 1)

    def monitor(self) -> None:
        """`up` once the gateway's GET /api answered 200 since this start (FR-SANDBOX-6 a)."""
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while not self.exiting:
            try:
                with opener.open(f"http://127.0.0.1:{GATEWAY_PORT}/api", timeout=1) as resp:
                    if resp.status == 200:
                        self.gateway_seen = True
                        return
            except OSError:
                pass
            time.sleep(0.2)

    def start_gateway(self) -> None:
        path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        env = {
            "HOME": "/home/lab",
            "HOSTNAME": os.environ.get("HOSTNAME") or socket.gethostname(),
            "LANG": "C.UTF-8",
            "PATH": ":".join(dict.fromkeys(p for p in path.split(":") if p)),
        }
        argv = [
            GATEWAY,
            "--KernelGatewayApp.ip=0.0.0.0",
            f"--KernelGatewayApp.port={GATEWAY_PORT}",
            "--KernelGatewayApp.port_retries=0",
            "--KernelGatewayApp.api=kernel_gateway.jupyter_websocket",
            "--KernelGatewayApp.auth_token=",
            "--KernelGatewayApp.max_kernels=4",
            "--KernelGatewayApp.default_kernel_name=python3",
            "--JupyterWebsocketPersonality.list_kernels=False",
        ]
        self.gateway = subprocess.Popen(  # noqa: S603
            argv, env=env, cwd="/home/lab", stdin=subprocess.DEVNULL, process_group=0, close_fds=True,
        )

    # -- control endpoint (FR-SANDBOX-6) ---------------------------------------------------------------------
    def state_body(self) -> bytes:
        return json.dumps({"nonce": self.nonce, "gateway": self.state}).encode()

    @staticmethod
    def respond(conn: socket.socket, code: int, body: bytes = b"") -> None:
        head = (
            f"HTTP/1.1 {code} {REASONS[code]}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        ).encode()
        try:
            conn.settimeout(0.5)
            conn.sendall(head + body)
        except OSError:
            pass

    def handle(self, conn: socket.socket, buf: bytearray) -> bool:
        """Returns True when the request is complete (answered) and the connection can be closed."""
        end = buf.find(b"\r\n\r\n")
        if end < 0:
            if len(buf) > MAX_HEADER:
                self.respond(conn, 413)
                return True
            return False
        lines = bytes(buf[:end]).split(b"\r\n")
        parts = lines[0].split(b" ")
        method, path = (parts[0], parts[1]) if len(parts) >= 2 else (b"", b"")
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(b":")
            headers[name.strip().lower()] = value.strip()
        if b"transfer-encoding" in headers:
            self.respond(conn, 413)
            return True
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            length = MAX_BODY + 1
        if length > MAX_BODY or length < 0:
            self.respond(conn, 413)
            return True
        if len(buf) - end - 4 < length:
            return False
        if method == b"GET" and path == b"/control/state":
            self.respond(conn, 200, self.state_body())
        elif method == b"POST" and path == b"/control/exit":
            self.exiting = True
            self.respond(conn, 202)
            self.begin_exit()
        else:
            self.respond(conn, 404)
        return True

    def serve(self, listeners: list[socket.socket]) -> None:
        sel = selectors.DefaultSelector()
        for lst in listeners:
            lst.setblocking(False)
            sel.register(lst, selectors.EVENT_READ, "listen")
        sel.register(self.wake_r, selectors.EVENT_READ, "wake")
        # conn -> (buffer, deadline, local, accepted_at)
        conns: dict[socket.socket, tuple[bytearray, float, bool, float]] = {}

        def close(conn: socket.socket) -> None:
            sel.unregister(conn)
            conns.pop(conn, None)
            conn.close()

        def admit(conn: socket.socket, peer: str) -> bool:
            local = is_local(conn, peer)
            same = [c for c, v in conns.items() if v[2] == local]
            if local and len(same) >= LOCAL_SLOTS:
                return False
            if not local and len(same) >= REMOTE_SLOTS:
                close(min(same, key=lambda c: conns[c][3]))
            now = time.monotonic()
            conns[conn] = (bytearray(), now + READ_TIMEOUT_S, local, now)
            sel.register(conn, selectors.EVENT_READ, "conn")
            return True

        while True:
            now = time.monotonic()
            nearest = min((v[1] for v in conns.values()), default=now + 1.0)
            for key, _ in sel.select(timeout=max(0.0, min(nearest - now, 1.0))):
                if key.data == "wake":
                    try:
                        os.read(self.wake_r, 64)
                    except BlockingIOError:
                        pass
                    self.begin_exit()
                    continue
                sock = key.fileobj
                if not isinstance(sock, socket.socket):
                    die(1)
                    return
                if key.data == "listen":
                    try:
                        conn, addr = sock.accept()
                    except OSError:
                        continue
                    conn.setblocking(False)
                    if not admit(conn, str(addr[0])):
                        conn.close()
                    continue
                try:
                    data = sock.recv(4096)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    close(sock)
                    continue
                if not data:
                    close(sock)
                    continue
                buf = conns[sock][0]
                buf += data
                if self.handle(sock, buf):
                    close(sock)
            now = time.monotonic()
            for conn in [c for c, v in conns.items() if v[1] <= now]:
                close(conn)


def listeners() -> list[socket.socket]:
    """0.0.0.0:8889, and [::]:8889 (IPV6_V6ONLY) whenever the container has IPv6 at all (FR-SANDBOX-4 c)."""
    v4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    v4.bind(("0.0.0.0", CONTROL_PORT))  # noqa: S104 - reachable only on the kernel's own network (header note)
    v4.listen(512)
    out = [v4]
    try:
        v6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        v6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        v6.bind(("::", CONTROL_PORT))
        v6.listen(512)
        out.append(v6)
    except OSError as e:
        if e.errno not in NO_IPV6:
            raise
    return out


def main() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        die(1)
    sys.excepthook = lambda *_: die(1)
    threading.excepthook = lambda _: die(1)
    sup = Supervisor()
    signal.signal(signal.SIGTERM, sup.on_sigterm)
    signal.signal(signal.SIGINT, lambda *_: die(1))
    socks = listeners()
    sup.start_gateway()
    for target in (sup.reaper, sup.watchdog, sup.monitor):
        threading.Thread(target=target, daemon=True).start()
    sup.serve(socks)


if __name__ == "__main__":
    try:
        main()
    finally:
        die(1)
