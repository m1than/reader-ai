#!/usr/bin/env bash
# Spike trace-backend (roadmap 0.3; ADR-0018). Per backend (phoenix, mlflow):
#   STARTS cold starts → idle memory and process count each →
#   probe run: 3 Agent SDK stages on claude-haiku-5-5, one seeded failure → nesting, timed triage, score via the backend API →
#   synthetic replay of 498 spans through Rotel (no model calls) → peak memory, ingest count → teardown (down -v).
# For ingest only Rotel's exporter settings differ between backends; scores and queries are backend-specific (query.py).
#   bash spikes/trace-backend/run.sh                 both backends
#   BACKENDS=mlflow STARTS=1 bash spikes/trace-backend/run.sh
# Spend cap: SPIKE_CAP_USD for the whole spike, summed over every results/*/spent.txt plus the hand-recorded exploratory
# runs. It is checked before the probe and passed to probe.py, which checks it between stages only: a stage that has
# started always finishes (one haiku stage costs well under $0.01).
# Token: the owner allowed Keychain use for this spike in chat (2026-11-20, "allow keychain for compile-cost and
# trace-backend"). It is piped to the probe on stdin, exported only inside the container, never printed, and checked
# to be absent from `docker inspect` (pattern from spikes/compile-cost).
set -euo pipefail
cd "$(dirname "$0")"
BACKENDS="${BACKENDS:-phoenix mlflow}"
STARTS="${STARTS:-3}"
SPIKE_CAP_USD="${SPIKE_CAP_USD:-2.00}"
EXPLORATORY_USD=0.003669   # hand-recorded: two exploratory probe runs whose result dirs were not kept (0.003082 + 0.000587)
OUT="results/$(date +%Y%m%dT%H%M%S)"
mkdir -p "$OUT"
NET=reader-spike-trace_traces
echo 0 > "$OUT/spent.txt"
sampler=""

cleanup() {
  [[ -n "$sampler" ]] && kill "$sampler" 2>/dev/null || true
  docker rm -f trace-probe trace-replay >/dev/null 2>&1 || true
  EXPORT_ENDPOINT=x EXPORT_PROTOCOL=x docker compose --profile phoenix --profile mlflow down -v >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

spent_total() { # every results dir (this one included) + exploratory
  python3 -c "import glob; print(round($EXPLORATORY_USD + sum(float(open(f).read() or 0) for f in glob.glob('results/*/spent.txt')), 6))"
}

stats() { # one JSON object: {container: {"mib": .., "pids": ..}}
  docker stats --no-stream --format '{{.Name}} {{.MemUsage}} {{.PIDs}}' "$@" | python3 -c '
import json, sys
out = {}
for line in sys.stdin:
    name, mem, *_rest = line.split()
    pids = line.split()[-1]
    v = float(mem.rstrip("GiBMK")) * (1024 if mem.endswith("GiB") else 1 / 1024 if mem.endswith("KiB") else 1)
    out[name] = {"mib": round(v, 1), "pids": int(pids)}
print(json.dumps(out))'
}

sample() { # file, containers... — samples every 1 s until killed
  local f="$1"; shift
  while true; do stats "$@" >> "$f" 2>/dev/null || true; sleep 1; done
}

peak() { # file → {container: {"mib": max, "pids": max}}
  python3 - "$1" <<'EOF'
import json, sys
peak = {}
for line in open(sys.argv[1]):
    if line.startswith("{"):
        for k, v in json.loads(line).items():
            p = peak.setdefault(k, {"mib": 0, "pids": 0})
            p["mib"], p["pids"] = max(p["mib"], v["mib"]), max(p["pids"], v["pids"])
print(json.dumps(peak))
EOF
}

up() { # backend → prints seconds until the API answers (images already pulled)
  docker compose --profile "$1" up -d >/dev/null 2>&1
  local t0; t0=$(date +%s)
  case $1 in
    phoenix) until curl -fsS -o /dev/null http://127.0.0.1:6006/v1/projects 2>/dev/null; do sleep 1; done ;;
    mlflow)  until curl -fsS -o /dev/null http://127.0.0.1:5000/health 2>/dev/null; do sleep 1; done ;;
  esac
  echo $(( $(date +%s) - t0 ))
}

docker build -q --platform linux/arm64 -t reader-spike-trace-probe . >/dev/null

for b in $BACKENDS; do
  case $b in
    phoenix) export EXPORT_ENDPOINT=http://phoenix:4317 EXPORT_PROTOCOL=grpc EXPORT_HEADERS= ;;
    mlflow)  export EXPORT_ENDPOINT=http://mlflow:5000 EXPORT_PROTOCOL=http EXPORT_HEADERS="x-mlflow-experiment-id=0" ;;
  esac
  C=(reader-spike-trace-rotel-1 "reader-spike-trace-$b-1")

  # 1. Idle memory over STARTS fresh starts (each `down -v` removes the backend's data).
  : > "$OUT/$b.idle.jsonl"
  for i in $(seq "$STARTS"); do
    ready=$(up "$b"); sleep 20
    printf '{"start": %d, "ready_s": %d, "stats": %s}\n' "$i" "$ready" "$(stats "${C[@]}")" >> "$OUT/$b.idle.jsonl"
    [[ $i -lt $STARTS ]] && docker compose --profile "$b" down -v >/dev/null 2>&1
  done

  # 2. Probe run (model calls), within the remaining spend.
  remaining=$(python3 -c "print(round($SPIKE_CAP_USD - $(spent_total), 6))")
  : > "$OUT/$b.probe.mem"
  sample "$OUT/$b.probe.mem" "${C[@]}" & sampler=$!
  token_keys_in_inspect=null
  if python3 -c "import sys; sys.exit(0 if $remaining > 0.05 else 1)"; then
    security find-generic-password -s reader-ai -a claude-oauth-token -w | docker run --rm -i --name trace-probe \
      --network "$NET" -e READER_OTLP_TRACES=http://rotel:4318/v1/traces -e RUN_ID="$b" -e CAP_USD="$remaining" \
      -e CLAUDE_CODE_ENABLE_TELEMETRY=1 -e CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1 \
      -e OTEL_TRACES_EXPORTER=otlp -e OTEL_METRICS_EXPORTER=none -e OTEL_LOGS_EXPORTER=none \
      -e OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf -e OTEL_EXPORTER_OTLP_ENDPOINT=http://rotel:4318 \
      -e CLAUDE_CODE_OTEL_DIAG_STDERR=1 \
      reader-spike-trace-probe > "$OUT/$b.probe.out" 2> "$OUT/$b.probe.stderr" &
    probe=$!
    for _ in $(seq 50); do docker inspect trace-probe >/dev/null 2>&1 && break; sleep 0.2; done
    # Count of env entries that name the token variable; the value itself is never printed.
    token_keys_in_inspect=$(docker inspect trace-probe --format '{{json .Config.Env}}' 2>/dev/null \
      | python3 -c 'import json,sys; print(sum(1 for e in json.load(sys.stdin) if e.startswith("CLAUDE_CODE_OAUTH_TOKEN")))' \
      || echo null)
    wait "$probe" || true
  else
    echo '{"skipped": "spend cap reached"}' > "$OUT/$b.probe.out"
  fi
  # Spend from the per-stage lines, so a killed probe's finished stages still count.
  run_spent=$(python3 - "$OUT/$b.probe.out" <<'EOF'
import json, sys
total = 0.0
for line in open(sys.argv[1]):
    try: total += float(json.loads(line).get("stage_row", {}).get("cost_usd") or 0)
    except ValueError: pass
print(round(total, 6))
EOF
)
  python3 -c "print(round(float(open('$OUT/spent.txt').read()) + $run_spent, 6))" > "$OUT/spent.txt.new" && mv "$OUT/spent.txt.new" "$OUT/spent.txt"
  sleep 15                                         # Rotel batch + backend ingest
  kill "$sampler"; sampler=""
  trace_id=$(grep '"trace_id"' "$OUT/$b.probe.out" | tail -1 | python3 -c 'import json,sys; print(json.load(sys.stdin)["trace_id"])' || echo none)
  python3 query.py "$b" "$trace_id" > "$OUT/$b.query.json" 2> "$OUT/$b.query.stderr" || echo '{}' > "$OUT/$b.query.json"

  # 3. Synthetic replay: 498 spans, no model calls.
  : > "$OUT/$b.replay.mem"
  sample "$OUT/$b.replay.mem" "${C[@]}" & sampler=$!
  docker run --rm --name trace-replay --network "$NET" --entrypoint python \
    -e READER_OTLP_TRACES=http://rotel:4318/v1/traces reader-spike-trace-probe /app/replay.py > "$OUT/$b.replay.out"
  sleep 20
  kill "$sampler"; sampler=""
  replay_trace=$(python3 -c "import json; print(json.load(open('$OUT/$b.replay.out'))['trace_id'])")
  python3 query.py "$b" "$replay_trace" --count > "$OUT/$b.replay.count.json"
  after=$(stats "${C[@]}")

  python3 - "$OUT" "$b" "$token_keys_in_inspect" "$run_spent" "$after" "$(peak "$OUT/$b.probe.mem")" "$(peak "$OUT/$b.replay.mem")" <<'EOF'
import json, sys
out, b, tok, spent, after, peak_probe, peak_replay = sys.argv[1:8]
lines = [json.loads(l) for l in open(f"{out}/{b}.probe.out") if l.startswith("{")]
summary = next((l for l in lines if "trace_id" in l), {})
result = {
    "backend": b,
    "idle": [json.loads(l) for l in open(f"{out}/{b}.idle.jsonl")],
    "probe": {"spent_usd": float(spent), "token_keys_in_docker_inspect": None if tok == "null" else int(tok),
              "stages": [l["stage_row"] for l in lines if "stage_row" in l], "trace_id": summary.get("trace_id"),
              "peak": json.loads(peak_probe)},
    "query": json.load(open(f"{out}/{b}.query.json")),
    "replay": {**json.load(open(f"{out}/{b}.replay.out")), "ingested": json.load(open(f"{out}/{b}.replay.count.json")),
               "peak": json.loads(peak_replay), "after": json.loads(after)},
}
json.dump(result, open(f"{out}/{b}.json", "w"), indent=1)
EOF
  rm -f "$OUT/$b".{idle.jsonl,probe.mem,replay.mem,probe.out,query.json,replay.out,replay.count.json}
  [[ -s "$OUT/$b.query.stderr" ]] || rm -f "$OUT/$b.query.stderr"
  python3 -c "
import json; r = json.load(open('$OUT/$b.json'))
print(json.dumps({'backend': r['backend'], 'idle_mib': [i['stats'] for i in r['idle']], 'probe_peak': r['probe']['peak'],
  'replay_peak': r['replay']['peak'], 'replayed': r['replay']['spans'], 'ingested': r['replay']['ingested']['span_count'],
  'token_in_inspect': r['probe']['token_keys_in_docker_inspect'], 'spent': r['probe']['spent_usd'],
  'nesting': {k: v for k, v in r['query'].get('nesting', {}).items() if k != 'edges'},
  'triage': {k: r['query'].get('triage', {}).get(k) for k in ('fetch_s', 'walk_s', 'stage')},
  'score_visible': r['query'].get('score', {}).get('visible')}))"
  docker compose --profile "$b" down -v >/dev/null 2>&1
done
echo "this_run_spent_usd=$(cat "$OUT/spent.txt") spike_total_usd=$(spent_total)"
