#!/bin/sh
# Starts the stub (upstream :8080, forward proxy :8081), runs the probe, stops the stub.
set -eu
: > /out/requests.jsonl
mkfifo /tmp/stub.in
python /spike/stub.py < /tmp/stub.in > /tmp/stub.log 2>&1 &
exec 3> /tmp/stub.in
until grep -q stub-ready /tmp/stub.log 2>/dev/null; do python -c 'import time; time.sleep(0.1)'; done
python /spike/probe.py "$@"
exec 3>&-
