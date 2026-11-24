#!/usr/bin/env bash
# Spike web-search-otel (SRV-FRESH FR-FRESH-13 with SRV-OBS FR-OBS-6): with observability on, does the bundled CLI's
# OpenTelemetry export of a WebSearch run carry the search query, the result titles or URLs, or the nested model's
# summary, when the content flags are 0 (SRV-OBS's cli_env_additions)? Zero credit, no real token, no network: the
# container runs with --network none; the CLI talks only to stubs on 127.0.0.1.
#   bash spikes/web-search-otel/run.sh
set -euo pipefail
cd "$(dirname "$0")"
IMAGE=reader-spike-web-search-otel
OUT="results/$(date +%Y%m%dT%H%M%S)"
mkdir -p "$OUT"
docker build -q --platform linux/arm64 -t "$IMAGE" . >/dev/null
# Canaries: random per run, never real data. HANDLE stands in for SRV-AGENT's run_token.
rnd() { python3 -c 'import secrets; print(secrets.token_hex(12))'; }
HANDLE=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
docker run --rm --platform linux/arm64 --network none --read-only \
  --tmpfs /home/worker:uid=1000,size=64m --tmpfs /tmp:uid=1000,size=64m --cap-drop ALL \
  --security-opt no-new-privileges -e HOME=/home/worker -e HANDLE="$HANDLE" \
  -e CANARY_QUERY="querycanary$(rnd)" -e CANARY_TITLE="titlecanary$(rnd)" -e CANARY_URL="urlcanary$(rnd)" \
  -e CANARY_SUMMARY="summarycanary$(rnd)" -e CANARY_SYSTEM="systemcanary$(rnd)" \
  -v "$PWD/$OUT:/out" "$IMAGE" ${MODES:-content-off content-unset tool-details tool-content}
echo "results in $OUT"
