#!/usr/bin/env bash
# Browser-free checks for the passkey spike: brings the stack up, exports Caddy's root CA, verifies TLS
# with curl against that CA only, and validates the WebAuthn options JSON. Leaves the stack running for the
# owner's browser part; teardown: `docker compose -f spikes/passkey-localhost/compose.yaml down -v`.
#   bash spikes/passkey-localhost/check.sh            (PORT=8443 ... if 443 is taken)
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
source "$REPO/e2e/live/lib.sh"
PORT="${PORT:-443}"
DATA="$REPO/data/spikes/passkey-localhost"
URL="https://localhost$([[ $PORT == 443 ]] || echo ":$PORT")"
mkdir -p "$DATA"
export PORT ORIGIN_PORT_SUFFIX="$([[ $PORT == 443 ]] || echo ":$PORT")"

docker compose -f "$HERE/compose.yaml" up -d --build >/dev/null 2>&1 || { echo "compose up failed"; exit 1; }
for _ in $(seq 30); do
  docker compose -f "$HERE/compose.yaml" cp caddy:/data/caddy/pki/authorities/local/root.crt "$DATA/caddy-root.crt" \
    >/dev/null 2>&1 && curl -fsS --cacert "$DATA/caddy-root.crt" "$URL/health" >/dev/null 2>&1 && break
  sleep 1
done

CA=(--cacert "$DATA/caddy-root.crt")
post() { curl -fsS "${CA[@]}" -H 'content-type: application/json' -d "$2" "$URL$1"; }

section "TLS through Caddy's internal CA ($URL)"
check "root CA exported" test -s "$DATA/caddy-root.crt"
check "root is Caddy's local authority" bash -c "openssl x509 -in '$DATA/caddy-root.crt' -noout -subject | grep -q 'Caddy Local Authority'"
check "TLS verifies against that root only" curl -fsS "${CA[@]}" "$URL/health"
check "TLS fails without it (not publicly trusted)" bash -c "! curl -fsS '$URL/health'"
check "leaf certificate covers localhost" bash -c "echo | openssl s_client -connect 127.0.0.1:$PORT -servername localhost -CAfile '$DATA/caddy-root.crt' 2>/dev/null | openssl x509 -noout -ext subjectAltName | grep -q 'DNS:localhost'"
check "page served" bash -c "curl -fsS ${CA[*]} '$URL/' | grep -q 'Passkey spike'"

section "WebAuthn options JSON"
check "registration: rp.id = localhost" bash -c "$(declare -f post); URL='$URL'; CA=(${CA[*]}); post /register/options '{\"user\":\"curl\"}' | jq -e '.publicKey.rp.id == \"localhost\"'"
check "registration: resident key + UV required" bash -c "$(declare -f post); URL='$URL'; CA=(${CA[*]}); post /register/options '{\"user\":\"curl\"}' | jq -e '.publicKey.authenticatorSelection | .residentKey == \"required\" and .userVerification == \"required\"'"
check "registration: challenge ≥ 32 bytes, ES256 offered" bash -c "$(declare -f post); URL='$URL'; CA=(${CA[*]}); post /register/options '{\"user\":\"curl\"}' | jq -e '(.publicKey.challenge | length) >= 43 and ([.publicKey.pubKeyCredParams[].alg] | index(-7) != null)'"
check "authentication: rpId = localhost, UV required, discoverable" bash -c "$(declare -f post); URL='$URL'; CA=(${CA[*]}); post /login/options '{}' | jq -e '.publicKey.rpId == \"localhost\" and .publicKey.userVerification == \"required\" and ((.publicKey.allowCredentials // []) | length) == 0'"
check "verify rejects a malformed registration" bash -c "$(declare -f post); URL='$URL'; CA=(${CA[*]}); f=\$(post /register/options '{\"user\":\"curl\"}' | jq -r .flow); ! post /register/verify \"{\\\"flow\\\":\\\"\$f\\\",\\\"credential\\\":{\\\"id\\\":\\\"x\\\",\\\"rawId\\\":\\\"x\\\",\\\"type\\\":\\\"public-key\\\",\\\"response\\\":{\\\"clientDataJSON\\\":\\\"e30\\\",\\\"attestationObject\\\":\\\"oA\\\"}}}\""
check "steps logged to data/spikes/passkey-localhost/log.jsonl" bash -c "jq -e -s 'map(.event) | index(\"register.fail\") != null' '$DATA/log.jsonl'"
summary
