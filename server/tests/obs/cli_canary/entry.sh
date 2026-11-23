#!/bin/sh
# CLI canary harness (SRV-OBS INV-OBS-2): starts the stub, runs the probe, stops the stub.
set -eu
: > /out/requests.jsonl
mkdir -p /out/otlp
mkfifo /tmp/stub.in
python /canary/stub.py < /tmp/stub.in > /tmp/stub.log 2>&1 &
exec 3> /tmp/stub.in
until grep -q stub-ready /tmp/stub.log 2>/dev/null; do python -c 'import time; time.sleep(0.1)'; done
python /canary/probe.py "$@"
exec 3>&-
