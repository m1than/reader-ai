#!/usr/bin/env bash
# Spike obs-stack (SRV-OBS). Zero credit, no model calls, no real secret, no published port, its own Compose project
# (reader-spike-obs); the running `reader` stack is not touched.
#   bash spikes/obs-stack/run.sh                    # both backends, DURATION_S=480 each
#   BACKENDS=sqlite DURATION_S=60 bash spikes/obs-stack/run.sh
set -euo pipefail
cd "$(dirname "$0")"
OUT="$PWD/results/$(date +%Y%m%dT%H%M%S)"
mkdir -p "$OUT"
export OUT
DC=(docker compose -p reader-spike-obs -f compose.yaml)
sampler=""
cleanup() { [[ -n "$sampler" ]] && kill "$sampler" 2>/dev/null || true
  "${DC[@]}" --profile sqlite --profile postgres --profile driver down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT

sample() { # file — every 2 s: name, memory, pids of every container of the project
  while true; do
    docker stats --no-stream --format '{{.Name}} {{.MemUsage}} {{.PIDs}}' \
      $(docker ps -q --filter label=com.docker.compose.project=reader-spike-obs) 2>/dev/null \
      | python3 -c 'import sys,json,time
units={"KiB":1/1024,"MiB":1,"GiB":1024,"B":1/1048576}
def mib(s):
    for u,f in units.items():
        if s.endswith(u): return round(float(s[:-len(u)])*f,1)
rows={}
for line in sys.stdin:
    p=line.split()
    rows[p[0]]={"mib":mib(p[1]),"pids":int(p[-1])}
print(json.dumps({"t":time.time(),"stats":rows}))' >> "$1" || true
    sleep 2
  done
}

"${DC[@]}" --profile sqlite --profile postgres --profile driver build -q
for b in ${BACKENDS:-sqlite postgres}; do
  export BACKEND="$b"
  "${DC[@]}" --profile "$b" up -d
  sample "$OUT/$b.mem.jsonl" & sampler=$!
  "${DC[@]}" --profile "$b" --profile driver run --rm driver > "$OUT/$b.driver.stdout" 2> "$OUT/$b.driver.stderr" || true
  [[ -f "$OUT/driver.json" ]] && mv "$OUT/driver.json" "$OUT/$b.driver.json"
  kill "$sampler"; sampler=""
  "${DC[@]}" logs --no-color gate > "$OUT/$b.gate.log" 2>&1 || true
  "${DC[@]}" logs --no-color "mlflow-$b" 2>&1 | tail -40 > "$OUT/$b.mlflow.tail.log" || true
  "${DC[@]}" --profile sqlite --profile postgres --profile driver down -v --remove-orphans >/dev/null 2>&1
done
echo "results in $OUT"
