#!/usr/bin/env bash
# Spike trace-backend (roadmap 0.3; ADR-0018). Per backend (phoenix, mlflow):
#   start Rotel + backend → idle memory → one probe run (3 stages, one seeded failure) with peak memory →
#   spans, nesting, error triage and a quality score through the backend's API → teardown (down -v).
# Only Rotel's exporter settings differ between backends. Spends Agent SDK credit (claude-haiku-5-5);
# a hard cap per probe run (CAP_USD) and for the whole spike (SPIKE_CAP_USD) is enforced.
#   bash spikes/trace-backend/run.sh            both backends
#   BACKENDS=phoenix bash spikes/trace-backend/run.sh
# Token: Keychain item reader-ai/claude-oauth-token → `docker run -e`, never printed. The owner allowed this read
# for compile-cost and trace-backend (2026-11-20). Known exposure: visible to `docker inspect` while the probe runs.
set -euo pipefail
cd "$(dirname "$0")"
BACKENDS="${BACKENDS:-phoenix mlflow}"
SPIKE_CAP_USD="${SPIKE_CAP_USD:-2.00}"
OUT="results/$(date +%Y%m%dT%H%M%S)"
mkdir -p "$OUT"
NET=reader-spike-trace_traces
spent_file="$OUT/spent.txt"; echo 0 > "$spent_file"

docker build -q --platform linux/arm64 -t reader-spike-trace-probe . >/dev/null

mem() { docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' "$@" | awk '{ v=$2
  if (v ~ /GiB/) { sub(/GiB/,"",v); v*=1024 } else if (v ~ /KiB/) { sub(/KiB/,"",v); v/=1024 } else { sub(/MiB/,"",v) }
  printf "\"%s\":%.1f,", $1, v }' | sed 's/,$//; s/^/{/; s/$/}/'; }

for b in $BACKENDS; do
  case $b in
    phoenix) export EXPORT_ENDPOINT=http://phoenix:4317 EXPORT_PROTOCOL=grpc EXPORT_HEADERS= ;;
    mlflow)  export EXPORT_ENDPOINT=http://mlflow:5000 EXPORT_PROTOCOL=http EXPORT_HEADERS="x-mlflow-experiment-id=0" ;;
  esac
  docker compose --profile "$b" up -d >/dev/null 2>&1
  t0=$(date +%s)
  case $b in
    phoenix) until curl -fsS -o /dev/null http://127.0.0.1:6006/v1/projects; do sleep 1; done ;;
    mlflow)  until curl -fsS -o /dev/null http://127.0.0.1:5000/health; do sleep 1; done ;;
  esac
  ready_s=$(( $(date +%s) - t0 ))
  sleep 20
  idle=$(mem reader-spike-trace-rotel-1 "reader-spike-trace-$b-1")

  spent=$(cat "$spent_file")
  remaining=$(python3 -c "print(max(0.0, $SPIKE_CAP_USD - $spent))")
  token="$(security find-generic-password -s reader-ai -a claude-oauth-token -w)"
  ( while true; do mem reader-spike-trace-rotel-1 "reader-spike-trace-$b-1"; echo; sleep 1; done ) > "$OUT/$b.mem" 2>/dev/null &
  sampler=$!
  CLAUDE_CODE_OAUTH_TOKEN="$token" docker run --rm --network "$NET" -e CLAUDE_CODE_OAUTH_TOKEN \
    -e READER_OTLP_TRACES=http://rotel:4318/v1/traces -e RUN_ID="$b" -e CAP_USD="$remaining" \
    -e CLAUDE_CODE_ENABLE_TELEMETRY=1 -e CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1 \
    -e OTEL_TRACES_EXPORTER=otlp -e OTEL_METRICS_EXPORTER=none -e OTEL_LOGS_EXPORTER=none \
    -e OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf -e OTEL_EXPORTER_OTLP_ENDPOINT=http://rotel:4318 \
    -e CLAUDE_CODE_OTEL_DIAG_STDERR=1 \
    reader-spike-trace-probe > "$OUT/$b.probe.json" 2> "$OUT/$b.probe.stderr" || true
  unset token
  sleep 15                                      # Rotel batch + backend ingest
  kill "$sampler" 2>/dev/null || true
  peak=$(python3 - "$OUT/$b.mem" <<'EOF'
import json, sys
peak = {}
for line in open(sys.argv[1]):
    line = line.strip()
    if line.startswith("{"):
        for k, v in json.loads(line).items(): peak[k] = max(peak.get(k, 0), v)
print(json.dumps(peak))
EOF
)
  python3 -c "print(round($(cat "$spent_file") + $(jq -r '.spent_usd // 0' "$OUT/$b.probe.json"), 6))" > "$spent_file"
  trace_id=$(jq -r .trace_id "$OUT/$b.probe.json")
  python3 query.py "$b" "$trace_id" > "$OUT/$b.query.json" 2> "$OUT/$b.query.stderr" || echo '{}' > "$OUT/$b.query.json"
  jq -n --arg b "$b" --argjson ready "$ready_s" --argjson idle "$idle" --argjson peak "$peak" \
    --slurpfile probe "$OUT/$b.probe.json" --slurpfile q "$OUT/$b.query.json" \
    '{backend: $b, ready_s: $ready, idle_mem_mib: $idle, peak_mem_mib: $peak, probe: $probe[0], query: $q[0]}' > "$OUT/$b.json"
  rm -f "$OUT/$b.mem" "$OUT/$b.probe.json" "$OUT/$b.query.json"
  jq -c '{backend, ready_s, idle_mem_mib, peak_mem_mib, spent: .probe.spent_usd, nesting: .query.nesting, score: .query.score.visible}' "$OUT/$b.json"
  [[ -n "${KEEP:-}" ]] || docker compose --profile "$b" down -v >/dev/null 2>&1   # KEEP=1: leave the stack up
done
echo "spike_spent_usd=$(cat "$spent_file")"
