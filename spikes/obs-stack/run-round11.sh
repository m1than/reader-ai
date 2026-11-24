#!/usr/bin/env bash
# Spike obs-stack, round 11: the no-egress test of SRV-OBS FR-OBS-33 (design review round 2, N9). A probe container
# joins exactly the spike MLflow's networks (obs and obs-ui, both `internal: true` like deploy's) and tries a TCP
# connect to an IP literal and a DNS lookup of an external name; the same probe on a non-internal network is the
# positive control. Zero credit; own project; the stack is stopped at exit.
#   bash spikes/obs-stack/run-round11.sh
set -euo pipefail
cd "$(dirname "$0")"
OUT="$PWD/results/$(date +%Y%m%dT%H%M%S)-round11-egress"
mkdir -p "$OUT"
export OUT BACKEND=sqlite CSP_CANDIDATE="default-src 'self'"
DC=(docker compose -p reader-spike-obs -f compose.yaml -f compose.catalog.yaml)
down() { "${DC[@]}" --profile sqlite down -v --remove-orphans >/dev/null 2>&1 || true; docker rm -f r11-probe >/dev/null 2>&1 || true
         docker network rm r11-open >/dev/null 2>&1 || true; }
trap down EXIT
down
"${DC[@]}" --profile sqlite up -d mlflow-sqlite >/dev/null 2>&1
NETS=$(docker inspect --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' reader-spike-obs-mlflow-sqlite-1)
echo "mlflow networks: $NETS" >"$OUT/egress.txt"
for n in $NETS; do echo "$n internal=$(docker network inspect --format '{{.Internal}}' "$n")" >>"$OUT/egress.txt"; done
PROBE='import json, socket
out = {}
try:
    socket.create_connection(("1.1.1.1", 443), timeout=5).close(); out["tcp_ip_literal"] = "connected"
except OSError as e:
    out["tcp_ip_literal"] = f"failed: {type(e).__name__}"
try:
    out["dns_external"] = "resolved " + socket.gethostbyname("example.com")
except OSError as e:
    out["dns_external"] = f"failed: {type(e).__name__}"
try:
    socket.create_connection(("mlflow", 5000), timeout=5).close(); out["mlflow_reachable"] = True
except OSError:
    out["mlflow_reachable"] = False
print(json.dumps(out))'
first=$(echo "$NETS" | awk '{print $1}')
docker create --name r11-probe --network "$first" python:3.14-slim-trixie python -c "$PROBE" >/dev/null
for n in $(echo "$NETS" | cut -d' ' -f2-); do [ -n "$n" ] && docker network connect "$n" r11-probe; done
docker start -a r11-probe >"$OUT/probe-internal.json"
docker rm -f r11-probe >/dev/null
docker network create r11-open >/dev/null
docker run --rm --network r11-open python:3.14-slim-trixie python -c "$PROBE" >"$OUT/probe-control.json"
cat "$OUT/egress.txt" "$OUT/probe-internal.json" "$OUT/probe-control.json"
echo "results in $OUT"
