#!/usr/bin/env bash
# Spike obs-stack, round 12 (design review round 3): R1 (params of an incomplete Run, a same-value re-log) and R2
# (the LoggedModel's reader.last_started tag) as raw REST; then the egress probe of round 11 again, this time after
# MLflow is healthy, so the probe is seen reaching MLflow on its networks. Zero credit; own project; no published
# port; internal networks only; the stack is stopped at exit.
#   bash spikes/obs-stack/run-round12.sh
set -euo pipefail
cd "$(dirname "$0")"
OUT="$PWD/results/$(date +%Y%m%dT%H%M%S)-round12-rest"
mkdir -p "$OUT"
export OUT BACKEND=sqlite CSP_CANDIDATE="default-src 'self'"
DC=(docker compose -p reader-spike-obs -f compose.yaml -f compose.catalog.yaml -f compose.phase2.yaml -f compose.round9.yaml)
down() {
  docker logs reader-spike-obs-mlflow-sqlite-1 >"$OUT/mlflow.log" 2>&1 || true
  "${DC[@]}" --profile sqlite --profile driver down -v --remove-orphans >/dev/null 2>&1 || true
  docker rm -f r12-probe >/dev/null 2>&1 || true
}
trap down EXIT
"${DC[@]}" --profile sqlite --profile driver build -q
down
"${DC[@]}" --profile sqlite up -d >/dev/null 2>&1
"${DC[@]}" --profile sqlite --profile driver run --rm -v "$PWD/round12.py:/spike/round12.py:ro" --entrypoint python \
  driver2 /spike/round12.py > "$OUT/data.stdout" 2> "$OUT/data.stderr"
# Egress probe on exactly mlflow's networks, MLflow healthy now (round12.py waited for /health).
NETS=$(docker inspect --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' reader-spike-obs-mlflow-sqlite-1)
PROBE='import json, socket, urllib.request
out = {}
for name, fn in (("tcp_ip_literal", lambda: socket.create_connection(("1.1.1.1", 443), timeout=5).close()),
                 ("dns_external", lambda: socket.gethostbyname("example.com")),
                 ("mlflow_health", lambda: urllib.request.urlopen("http://mlflow:5000/health", timeout=5).status)):
    try:
        out[name] = {"ok": True, "value": fn()}
    except Exception as e:
        out[name] = {"ok": False, "error": type(e).__name__}
print(json.dumps(out))'
first=$(echo "$NETS" | awk '{print $1}')
docker create --name r12-probe --network "$first" python:3.14-slim-trixie python -c "$PROBE" >/dev/null
for n in $(echo "$NETS" | cut -d' ' -f2-); do [ -n "$n" ] && docker network connect "$n" r12-probe; done
docker start -a r12-probe >"$OUT/probe-internal.json"
echo "networks: $NETS" >"$OUT/egress.txt"
echo "results in $OUT"
