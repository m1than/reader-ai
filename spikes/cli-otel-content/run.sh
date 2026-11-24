#!/usr/bin/env bash
# Spike cli-otel-content (SRV-OBS): which CLI environment flags keep prompts, system prompts, responses and the
# credential out of the CLI's OpenTelemetry export, under SRV-AGENT's exact CLI environment. Zero credit, no real
# token, no network: the container runs with --network none; the CLI talks only to stubs on 127.0.0.1.
#   bash spikes/cli-otel-content/run.sh
set -euo pipefail
cd "$(dirname "$0")"
IMAGE=reader-spike-cli-otel-content
OUT="results/$(date +%Y%m%dT%H%M%S)"
mkdir -p "$OUT"
docker build -q --platform linux/arm64 -t "$IMAGE" . >/dev/null
# Canaries: random per run, never real data. HANDLE stands in for SRV-AGENT's run_token.
rnd() { python3 -c 'import secrets; print(secrets.token_hex(12))'; }
HANDLE=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
docker run --rm --platform linux/arm64 --network none --read-only \
  --tmpfs /home/worker:uid=1000,size=64m --tmpfs /tmp:uid=1000,size=64m --cap-drop ALL \
  --security-opt no-new-privileges -e HOME=/home/worker -e HANDLE="$HANDLE" \
  -e CANARY_PROMPT="bookcanary$(rnd)" -e CANARY_SYSTEM="systemcanary$(rnd)" -e CANARY_RESPONSE="answercanary$(rnd)" \
  -v "$PWD/$OUT:/out" "$IMAGE" ${MODES:-agent-env agent-env-explicit-off positive-control}
echo "results in $OUT"
