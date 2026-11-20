#!/usr/bin/env bash
# Spike compose-secrets-ro (OPS-DEPLOY review B7; ADR-0011, ADR-0023, proposed ADR-0026).
# Runs every variant with a fresh canary (CANARY- + 32 hex = 39 characters), checks where the value can be
# found, restarts, tears down. Writes results/<run-id>/<variant>.json with numbers and booleans only; the
# canary value is never printed or written to the repository.
#   bash spikes/compose-secrets-ro/run.sh
set -uo pipefail
cd "$(dirname "$0")"
OUT="results/$(date +%Y%m%dT%H%M%S)"
mkdir -p "$OUT"
SRC_DIR="$(mktemp -d)"            # 0700, outside the repository: the source of the file-sourced secret (d)
chmod 700 "$SRC_DIR"
LOGS="$HOME/Library/Containers/com.docker.docker/Data/log"
T0=$(date +%s)
IMG=python:3.14-slim-trixie
FILES=(compose.yaml compose.b-writable.yaml compose.c-exec.yaml compose.d-file.yaml compose.e-dep-c.yaml compose.e-dep-d.yaml)

canary() { echo "CANARY-$(uuidgen | tr -d -)"; }
dc() { local f="$1"; shift; SPIKE_SECRET_FILE="$SRC_DIR/tok" SPIKE_TOK="${SPIKE_TOK:-unset}" docker compose -f "$f" "$@"; }
teardown() { for f in "${FILES[@]}"; do dc "$f" down -v >/dev/null 2>&1; done; rm -rf "$SRC_DIR"; }
trap teardown EXIT
teardown_one() { dc "$1" down -v >/dev/null 2>&1; }

# Value occurrences outside the consumer: inspect, rendered config, files below /run/secrets in the container
# layer, exported filesystem, daemon events since the run started, Docker Desktop host/vm logs.
leaks() { # file container value
  local f="$1" id="$2" v="$3"
  jq -n \
    --argjson inspect "$(docker inspect "$id" | grep -c -- "$v")" \
    --argjson config "$(dc "$f" config 2>/dev/null | grep -c -- "$v")" \
    --argjson layer_paths_below_run_secrets "$(docker diff "$id" | grep -c '^A /run/secrets/.')" \
    --argjson export "$(docker export "$id" | grep -a -c -- "$v")" \
    --argjson events "$(docker events --since "$T0" --until "$(date +%s)" 2>/dev/null | grep -c -- "$v")" \
    --argjson desktop_log_files "$(grep -r -a -l -- "$v" "$LOGS" 2>/dev/null | wc -l | tr -d ' ')" \
    --argjson inspect_shows_source_path "$(docker inspect "$id" | grep -c -- "$SRC_DIR")" \
    '{inspect: $inspect, config: $config, layer_paths_below_run_secrets: $layer_paths_below_run_secrets,
      export: $export, events: $events, desktop_log_files: $desktop_log_files,
      inspect_shows_source_path: ($inspect_shows_source_path > 0)}'
}
consumer_view() { # file -> the consumer's own JSON-ish report (no value inside)
  dc "$1" logs --no-log-prefix svc 2>/dev/null | grep -F "'uid'" | tail -1 |
    python3 -c 'import ast,json,sys; s=sys.stdin.read().strip(); print(json.dumps(ast.literal_eval(s)) if s else "null")'
}
deliver() { # file value name atomic(0|1) -> exit code; value only on stdin
  local cmd="umask 077; cat > /run/secrets/$3"
  [[ "$4" == 1 ]] && cmd="umask 077; cat > /run/secrets/.$3.tmp && mv /run/secrets/.$3.tmp /run/secrets/$3"
  printf %s "$2" | dc "$1" exec -T svc sh -c "$cmd"
}
has_file() { dc "$1" exec -T svc sh -c "test -s /run/secrets/$2" >/dev/null 2>&1 && echo true || echo false; }
wait_log() { for _ in $(seq 40); do dc "$1" logs svc 2>/dev/null | grep -qF "'uid'" && return 0; sleep 0.25; done; return 1; }

echo "== (a) env-sourced secret, read-only root"
V=$(canary); f=compose.yaml
msg=$(SPIKE_TOK="$V" dc "$f" up -d 2>&1); rc=$?
jq -n --argjson rc "$rc" --argjson refused "$(grep -c 'sole supported option' <<<"$msg")" --argjson len "${#V}" \
  '{variant: "a", up_exit: $rc, refused_sole_file_option: ($refused > 0), canary_length: $len}' > "$OUT/a.json"
teardown_one "$f"; cat "$OUT/a.json"

echo "== (b) env-sourced secret, writable root"
V=$(canary); f=compose.b-writable.yaml
SPIKE_TOK="$V" dc "$f" up -d >/dev/null 2>&1; wait_log "$f"; id=$(dc "$f" ps -q svc)
view=$(consumer_view "$f"); L=$(SPIKE_TOK="$V" leaks "$f" "$id" "$V")
docker stop "$id" >/dev/null; export_after_stop=$(docker export "$id" | grep -a -c -- "$V"); docker start "$id" >/dev/null; sleep 1
SPIKE_TOK="$V" dc "$f" restart svc >/dev/null 2>&1; sleep 1
jq -n --argjson view "$view" --argjson leaks "$L" --argjson after_stop "$export_after_stop" --argjson kept "$(has_file "$f" tok)" \
  '{variant: "b", consumer: $view, found: $leaks, export_after_stop: $after_stop, file_after_restart: $kept}' > "$OUT/b.json"
teardown_one "$f"; cat "$OUT/b.json"

echo "== (c) read-only root, tmpfs /run/secrets, atomic exec delivery"
V=$(canary); f=compose.c-exec.yaml
dc "$f" up -d >/dev/null 2>&1; id=$(dc "$f" ps -q svc)
deliver "$f" "$V" tok 1; drc=$?; wait_log "$f"
view=$(consumer_view "$f"); L=$(leaks "$f" "$id" "$V")
swapmax=$(dc "$f" exec -T svc cat /sys/fs/cgroup/memory.swap.max 2>/dev/null | tr -d '\r\n')
root_read=$(dc "$f" exec -T -u 0 svc cat /run/secrets/tok >/dev/null 2>&1 && echo true || echo false)
cap=0; for i in $(seq 17); do printf x | dc "$f" exec -T svc sh -c "cat > /run/secrets/fill$i" 2>/dev/null && cap=$i; done
dc "$f" exec -T svc sh -c 'rm -f /run/secrets/fill*' >/dev/null 2>&1
race() { # atomic -> trials where a waiting reader saw the full value
  local ok=0 len
  for i in $(seq 20); do
    dc "$f" exec -T svc sh -c 'rm -f /run/secrets/r /run/secrets/.r.tmp /tmp/len' >/dev/null 2>&1
    dc "$f" exec -d svc python -c $'import os\np="/run/secrets/r"\nwhile not os.path.exists(p): pass\nopen("/tmp/len","w").write(str(len(open(p).read())))'
    sleep 0.3; deliver "$f" "$V" r "$1" >/dev/null 2>&1; sleep 0.4
    len=$(dc "$f" exec -T svc cat /tmp/len 2>/dev/null)
    [[ "$len" == "${#V}" ]] && ok=$((ok + 1))
  done
  echo "$ok"
}
naive=$(race 0); atomic=$(race 1)
dc "$f" restart svc >/dev/null 2>&1; sleep 2
jq -n --argjson view "$view" --argjson leaks "$L" --argjson drc "$drc" --arg swapmax "$swapmax" --argjson root "$root_read" \
  --argjson cap "$cap" --argjson naive "$naive" --argjson atomic "$atomic" --argjson kept "$(has_file "$f" tok)" \
  '{variant: "c", delivery_exit: $drc, consumer: $view, found: $leaks, memory_swap_max: $swapmax,
    root_without_caps_can_read: $root, small_files_fitting_in_64k_tmpfs: $cap,
    race_full_reads_of_20: {naive: $naive, atomic: $atomic}, file_after_restart: $kept}' > "$OUT/c.json"
teardown_one "$f"; cat "$OUT/c.json"

echo "== (d) read-only root, file-sourced Compose secret"
V=$(canary); f=compose.d-file.yaml
( umask 077; printf %s "$V" > "$SRC_DIR/tok" )
src_mode=$(stat -f '%Lp' "$SRC_DIR/tok"); dir_mode=$(stat -f '%Lp' "$SRC_DIR")
dc "$f" up -d >/dev/null 2>&1; wait_log "$f"; id=$(dc "$f" ps -q svc)
view=$(consumer_view "$f"); L=$(leaks "$f" "$id" "$V")
dc "$f" restart svc >/dev/null 2>&1; sleep 2
jq -n --argjson view "$view" --argjson leaks "$L" --arg sm "$src_mode" --arg dm "$dir_mode" --argjson kept "$(has_file "$f" tok)" \
  '{variant: "d", host_source_mode: $sm, host_source_dir_mode: $dm, consumer: $view, found: $leaks, file_after_restart: $kept}' > "$OUT/d.json"
teardown_one "$f"; cat "$OUT/d.json"

echo "== (e) depends_on service_healthy"
f=compose.e-dep-c.yaml; V=$(canary)
s=$(date +%s); dc "$f" up -d >/dev/null 2>&1; after_rc=$?; after_s=$(( $(date +%s) - s )); teardown_one "$f"
s=$(date +%s); dc "$f" up -d >/dev/null 2>&1 & up_pid=$!
for _ in $(seq 100); do id=$(dc "$f" ps -q svc 2>/dev/null); [[ -n "$id" ]] && [[ "$(docker inspect -f '{{.State.Running}}' "$id" 2>/dev/null)" == true ]] && break; sleep 0.1; done
deliver "$f" "$V" tok 1 >/dev/null 2>&1; wait "$up_pid"; conc_rc=$?; conc_s=$(( $(date +%s) - s )); teardown_one "$f"
f=compose.e-dep-d.yaml; V=$(canary); ( umask 077; printf %s "$V" > "$SRC_DIR/tok" )
s=$(date +%s); dc "$f" up -d >/dev/null 2>&1; d_rc=$?; d_s=$(( $(date +%s) - s )); teardown_one "$f"
jq -n --argjson a "$after_rc" --argjson as "$after_s" --argjson c "$conc_rc" --argjson cs "$conc_s" --argjson d "$d_rc" --argjson ds "$d_s" \
  '{variant: "e", c_delivery_after_up: {up_exit: $a, seconds: $as}, c_delivery_concurrent_polling: {up_exit: $c, seconds: $cs},
    d_file_secret: {up_exit: $d, seconds: $ds}}' > "$OUT/e.json"
cat "$OUT/e.json"

echo "== VM swap facts"
swaps=$(docker run --rm --network none "$IMG" cat /proc/swaps | tail -n +2 | awk '{print $1" size_kb="$3" used_kb="$4}')
noswap=$(docker run --rm --network none --tmpfs /x:noswap "$IMG" true 2>&1 | grep -c 'invalid tmpfs option')
jq -n --arg swaps "$swaps" --argjson noswap_rejected "$noswap" '{vm_swaps: $swaps, tmpfs_noswap_rejected: ($noswap_rejected > 0)}' > "$OUT/vm.json"
cat "$OUT/vm.json"
