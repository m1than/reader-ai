#!/usr/local/bin/python3 -I
"""Supervisor, PID 1 of reader-kernel:local (spec/modules/OPS-SANDBOX.yaml FR-SANDBOX-4..9).

It holds a start nonce in memory, starts Kernel Gateway as its child and serves the control endpoint on port 8889:
`GET /control/state` and `POST /control/exit`. It is fail-stop: a gateway exit, an uncaught exception, SIGINT or the
end of its control loop ends PID 1, which ends every process in the container; `restart: always` then brings a fresh
container with a new nonce (ADR-0022 reset without a Docker socket). Standard library only.
"""

import ctypes
import errno
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
GRACE_S = 2.0          # FR-SANDBOX-6 (b)
READ_TIMEOUT_S = 5.0   # FR-SANDBOX-6 (c)
MAX_BODY = 1024        # FR-SANDBOX-6 (c)
MAX_HEADER = 8192
MAX_CONNECTIONS = 256  # beyond this, new connections are closed at once; the loop never blocks on them
PR_SET_DUMPABLE = 4
NO_IPV6 = {errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL, errno.EPROTONOSUPPORT}
REASONS = {200: "OK", 202: "Accepted", 404: "Not Found", 413: "Content Too Large"}


def die(code: int) -> None:
    os._exit(code)


class Supervisor:
    def __init__(self) -> None:
        self.nonce = os.urandom(16).hex()
        self.state = "starting"
        self.lock = threading.Lock()
        self.exit_armed = threading.Event()
        self.gateway: subprocess.Popen[bytes] | None = None

    # -- exit path (FR-SANDBOX-6 b) --------------------------------------------------------------------------
    def mark_exiting(self) -> None:
        with self.lock:
            self.state = "exiting"

    def begin_exit(self) -> None:
        self.mark_exiting()
        if self.gateway is None:
            die(0)
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
        """Reap every child, orphans included; only the gateway's exit ends PID 1."""
        while True:
            try:
                pid, _ = os.waitpid(-1, 0)
            except ChildProcessError:
                time.sleep(0.05)
                continue
            except InterruptedError:
                continue
            if self.gateway is not None and pid == self.gateway.pid:
                with self.lock:
                    exiting = self.state == "exiting"
                die(0 if exiting else 1)

    def monitor(self) -> None:
        """`up` once the gateway's GET /api answered 200 since this start (FR-SANDBOX-6 a)."""
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while True:
            with self.lock:
                if self.state != "starting":
                    return
            try:
                with opener.open(f"http://127.0.0.1:{GATEWAY_PORT}/api", timeout=1) as resp:
                    if resp.status == 200:
                        with self.lock:
                            if self.state == "starting":
                                self.state = "up"
                        return
            except OSError:
                pass
            time.sleep(0.2)

    def start_gateway(self) -> None:
        env = {
            "HOME": "/home/lab",
            "HOSTNAME": os.environ.get("HOSTNAME") or socket.gethostname(),
            "LANG": "C.UTF-8",
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
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
        with self.lock:
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
            self.mark_exiting()
            self.respond(conn, 202)
            self.begin_exit()
        else:
            self.respond(conn, 404)
        return True

    def serve(self, listeners: list[socket.socket]) -> None:
        sel = selectors.DefaultSelector()
        for lst in listeners:
            lst.setblocking(False)
            sel.register(lst, selectors.EVENT_READ, None)
        conns: dict[socket.socket, tuple[bytearray, float]] = {}

        def close(conn: socket.socket) -> None:
            sel.unregister(conn)
            conns.pop(conn, None)
            conn.close()

        while True:
            now = time.monotonic()
            nearest = min((deadline for _, deadline in conns.values()), default=now + 1.0)
            for key, _ in sel.select(timeout=max(0.0, min(nearest - now, 1.0))):
                sock = key.fileobj
                assert isinstance(sock, socket.socket)
                if key.data is None:
                    try:
                        conn, _ = sock.accept()
                    except OSError:
                        continue
                    if len(conns) >= MAX_CONNECTIONS:
                        conn.close()
                        continue
                    conn.setblocking(False)
                    conns[conn] = (bytearray(), time.monotonic() + READ_TIMEOUT_S)
                    sel.register(conn, selectors.EVENT_READ, "conn")
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
                buf, deadline = conns[sock]
                buf += data
                if self.handle(sock, buf):
                    close(sock)
            now = time.monotonic()
            for conn in [c for c, (_, deadline) in conns.items() if deadline <= now]:
                close(conn)


def listeners() -> list[socket.socket]:
    """0.0.0.0:8889, and [::]:8889 (IPV6_V6ONLY) whenever the container has IPv6 at all (FR-SANDBOX-4 c)."""
    v4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    v4.bind(("0.0.0.0", CONTROL_PORT))  # noqa: S104 - reachable only on the kernel's own network (header note)
    v4.listen(128)
    out = [v4]
    try:
        v6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        v6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        v6.bind(("::", CONTROL_PORT))
        v6.listen(128)
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
    signal.signal(signal.SIGTERM, lambda *_: sup.begin_exit())
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
