"""Spike stand-in for the api's owner gate (never shipped).

GET /api/auth/owner-gate (what Caddy's forward_auth sends):
  403 when an `Origin` header is present and is not an allowed dashboard origin, or when the original method
      (X-Forwarded-Method) is not GET/HEAD and `Origin` is not exactly an allowed dashboard origin (SRV-OBS review B2);
  then 204 for the cookie `__Host-reader_session=owner`, 403 for `=learner`, 401 otherwise.
Allowed origins: EXPECT_ORIGINS (comma-separated); the spike serves plain HTTP, so http://localhost:8443 here.

Port 9001 is an echo upstream: it answers 200 with the received header names as JSON and sets a cookie and an ACAO
header, so the spike sees whether Caddy strips Cookie/Authorization going up and Set-Cookie/Access-Control-* coming down.
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ALLOWED = {o for o in os.environ.get("EXPECT_ORIGINS", "http://localhost:8443").split(",") if o}
# Round 5 (review B8): per-site origins. When SITE_ORIGINS is set (`mlflow=<origin>|<origin>,jaeger=<origin>`), the
# allowed origins are those of the site named by X-Reader-Site (set by Caddy); no or an unknown site is refused.
SITE_ORIGINS = {k: set(v.split("|")) for k, v in (p.split("=", 1) for p in os.environ.get("SITE_ORIGINS", "").split(",")
                                                   if "=" in p)}


class Gate(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        cookies = dict(p.strip().split("=", 1) for p in self.headers.get("Cookie", "").split(";") if "=" in p)
        who = cookies.get("__Host-reader_session")
        origin = self.headers.get("Origin")
        method = (self.headers.get("X-Forwarded-Method") or "GET").upper()
        site = self.headers.get("Sec-Fetch-Site")
        mode = self.headers.get("Sec-Fetch-Mode")
        dest = self.headers.get("Sec-Fetch-Dest")
        top_nav = site == "same-site" and mode == "navigate" and dest == "document" and method == "GET"
        allowed = SITE_ORIGINS.get(self.headers.get("X-Reader-Site") or "", set()) if SITE_ORIGINS else ALLOWED
        if SITE_ORIGINS and not allowed:
            status = 403
        elif origin is not None and origin not in allowed:
            status = 403
        elif method not in ("GET", "HEAD") and origin not in allowed:
            status = 403
        elif site is not None and site not in ("same-origin", "none") and not top_nav:
            status = 403  # review round 2 N1: only a top-level GET navigation may arrive same-site
        else:
            status = 204 if who == "owner" else 403 if who == "learner" else 401
        print(json.dumps({"path": self.path, "status": status, "fwd_method": method, "origin": origin,
                          "reader_site": self.headers.get("X-Reader-Site"),
                          "sec_fetch": [site, mode, dest], "fwd_uri": self.headers.get("X-Forwarded-Uri")}),
              file=sys.stderr, flush=True)
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()


class Echo(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def _echo(self) -> None:
        body = json.dumps({"header_names": sorted(k.lower() for k in self.headers.keys())}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", "upstream=1; Path=/")
        self.send_header("Access-Control-Allow-Origin", "http://evil.localhost:3000")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _echo
    do_POST = _echo


for port, handler in ((9000, Gate), (9001, Echo)):
    threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", port), handler).serve_forever, daemon=True).start()
threading.Event().wait()
