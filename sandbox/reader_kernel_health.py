#!/usr/local/bin/python3 -I
"""Health command of reader-kernel:local (OPS-SANDBOX FR-SANDBOX-7): exit 0 only when the supervisor reports
`gateway: up` within 2 s. Prints one word: the state, `unreachable` or `unknown`."""

import json
import sys
import urllib.request

try:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://127.0.0.1:8889/control/state", timeout=2) as resp:
        state = str(json.loads(resp.read(1024)).get("gateway", "unknown"))
except (OSError, ValueError):
    state = "unreachable"
if state not in ("starting", "up", "exiting", "unreachable"):
    state = "unknown"
print(state)
sys.exit(0 if state == "up" else 1)
