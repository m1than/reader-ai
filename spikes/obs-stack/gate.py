"""Spike stand-in for the api's owner gate (never shipped). GET /api/auth/owner-gate: 204 when the Cookie header
carries `__Host-reader_session=owner`, 403 for `=learner`, 401 otherwise. Logs the forwarded method and URI only."""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Gate(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        cookies = dict(p.strip().split("=", 1) for p in self.headers.get("Cookie", "").split(";") if "=" in p)
        who = cookies.get("__Host-reader_session")
        status = 204 if who == "owner" else 403 if who == "learner" else 401
        print(json.dumps({"path": self.path, "status": status, "fwd_method": self.headers.get("X-Forwarded-Method"),
                          "fwd_uri": self.headers.get("X-Forwarded-Uri")}), file=sys.stderr, flush=True)
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()


ThreadingHTTPServer(("0.0.0.0", 9000), Gate).serve_forever()
