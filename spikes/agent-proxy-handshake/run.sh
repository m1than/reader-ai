#!/usr/bin/env bash
# Spike agent-proxy-handshake (SRV-AGENT review B1). Zero credit, no real token, no network:
# the container runs with --network none; the CLI talks only to the stub on 127.0.0.1.
#   bash spikes/agent-proxy-handshake/run.sh
#   STUB_SCENARIO=websearch MODES="websearch websearch-deny" TAG=websearch bash spikes/agent-proxy-handshake/run.sh
#     (spikes/web-search § 5a; add STUB_WS_ERROR=max_uses_exceeded for the error case, MODEL=… for another model)
set -euo pipefail
cd "$(dirname "$0")"
IMAGE=reader-spike-agent-proxy-handshake
OUT="results/$(date +%Y%m%dT%H%M%S)"
mkdir -p "$OUT"
docker build -q --platform linux/arm64 -t "$IMAGE" . >/dev/null
# A handle-shaped placeholder: 32 random bytes, urlsafe base64 (43 chars), like SRV-AGENT FR-AGENT-3.
HANDLE=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
docker run --rm --platform linux/arm64 --network none --read-only \
  --tmpfs /home/worker:uid=1000,size=64m --tmpfs /tmp:uid=1000,size=64m --cap-drop ALL \
  --security-opt no-new-privileges -e HANDLE="$HANDLE" -e EXPECT_HANDLE="$HANDLE" -e HOME=/home/worker \
  ${NONESSENTIAL_OFF:+-e NONESSENTIAL_OFF=1} ${MAX_OUT:+-e MAX_OUT="$MAX_OUT"} \
  ${STUB_ERROR_STATUS:+-e STUB_ERROR_STATUS="$STUB_ERROR_STATUS"} \
  ${STUB_SCENARIO:+-e STUB_SCENARIO="$STUB_SCENARIO"} ${STUB_WS_ERROR:+-e STUB_WS_ERROR="$STUB_WS_ERROR"} \
  ${MODEL:+-e MODEL="$MODEL"} \
  -v "$PWD/$OUT:/out" "$IMAGE" ${MODES:-tools-empty tools-default}
if [[ -n "${TAG:-}" ]]; then mv "$OUT" "$OUT-$TAG"; OUT="$OUT-$TAG"; fi
echo "results in $OUT"
