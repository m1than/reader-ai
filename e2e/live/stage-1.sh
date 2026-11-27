#!/usr/bin/env bash
# Stage-1 live check (docs/roadmap.md stage 1; [live] items of OPS-DEPLOY, OPS-CLI and SRV-AUTH; ADR-0026 verified_by).
#
# Precondition: the owner ran `reader up --config <path>` and the stack is up. The owner's reader.yaml declares the
# learner `live-check` (deploy/reader.example.yaml shows it).
# Judge lane (ADR-0039 B7b, OPS-DEPLOY FR-DEPLOY-16): the owner's reader.yaml binds the judge slots, at least
# `tasks: { judge.find: { model: haiku } }` (v1 bindings in deploy/reader.example.yaml), else `reader doctor` fails
# judge_slots. A stack first started before FR-DEPLOY-16 has an `agentnet` without ipam, which Compose does not change in
# place: run `reader down` (volumes are kept) and then `reader up` once, so agentnet is recreated with 10.230.7.0/24.
#
#   bash e2e/live/stage-1.sh --config ~/path/to/reader.yaml           # non-destructive checks (≈ 5 min)
#   bash e2e/live/stage-1.sh --config ~/path/to/reader.yaml --drill   # plus the backup-restore drill (DESTRUCTIVE)
#
# Output: one PASS/FAIL line per check, then `STAGE 1: PASS|FAIL`. Nothing here prints a secret, a token or the
# environment. e2e/live/** is outside the spec gate (tools/lib/repo.mjs classifies it as neither code nor test).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source=e2e/live/lib.sh
source "$ROOT/e2e/live/lib.sh"

CONFIG=""
DRILL=0
while (($#)); do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --drill)
      # Disabled until S1.OPS-CLI-restore-seed: restore refuses the rows the api seeds from reader.yaml, so the drill
      # would delete the volumes and then fail to restore.
      echo "--drill is disabled until task S1.OPS-CLI-restore-seed lands (restore would fail after deleting volumes)" >&2
      exit 2 ;;
    *) echo "usage: $0 --config PATH [--drill]" >&2; exit 2 ;;
  esac
done
[[ -n "$CONFIG" && -f "$CONFIG" ]] || { echo "usage: $0 --config PATH [--drill]  (reader.yaml not found)" >&2; exit 2; }
for tool in reader docker jq curl uv; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 2; }
done

WORK="$(mktemp -d "${TMPDIR:-/tmp}/reader-stage1.XXXXXX")"
chmod 700 "$WORK"
trap 'rm -rf "$WORK"' EXIT
CACERT="$WORK/caddy-root.crt"   # public root certificate, not a secret

PROJECT="$(reader status --config "$CONFIG" --json 2>/dev/null | jq -r '.project // empty' || true)"
PROJECT="${PROJECT:-reader}"
dc() { docker compose -p "$PROJECT" "$@"; }
CONSUMERS="postgres api worker worker-judge"   # default-profile secret consumers (mcp, agent-proxy: later stages)
PY() { (cd "$ROOT/server" && uv run --quiet python -I "$@"); }

# Runs a helper that prints `PASS name` / `FAIL name` lines and folds them into lib.sh's counters.
subchecks() {
  local line out rc=0
  out="$("$@" 2>&1)" || rc=$?
  while IFS= read -r line; do
    case "$line" in
      PASS\ *) printf '  \033[32mPASS\033[0m %s\n' "${line#PASS }"; LIVE_PASS=$((LIVE_PASS + 1)) ;;
      FAIL\ *) printf '  \033[31mFAIL\033[0m %s\n' "${line#FAIL }"; LIVE_FAIL=$((LIVE_FAIL + 1)); LIVE_FAILED+=("${line#FAIL }") ;;
      SKIP\ *) printf '  SKIP %s\n' "${line#SKIP }" ;;
      *) ;;  # helpers print nothing else; anything unexpected is dropped rather than echoed
    esac
  done <<<"$out"
  if ((rc != 0)) && ! grep -q '^FAIL ' <<<"$out"; then
    printf '  \033[31mFAIL\033[0m %s\n' "helper $2 exited $rc"; LIVE_FAIL=$((LIVE_FAIL + 1)); LIVE_FAILED+=("helper $2")
  fi
}

health_of() { docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$(dc ps -q "$1")"; }
healthy_with_file_within() { local s="$1" n="$2" _
  for _ in $(seq "$n"); do
    [[ "$(health_of "$s")" == healthy ]] && dc exec -T "$s" test -s /run/secrets/db-password && return 0
    sleep 2
  done; return 1; }
reader_up() { reader up --config "$CONFIG" </dev/null >/dev/null 2>&1; }   # stdin closed: no interactive trust prompt

# --- Stack ------------------------------------------------------------------------------------------------------
section "Stack (OPS-DEPLOY, OPS-CLI)"
# Every listed service running and healthy, and the four default-profile services among them (the `kernels` profile,
# FR-CLI-5, adds kernel-user-<handle>, kernel-verify, kernel-agent and kernel-pair-*).
four_healthy_and_watcher() {
  reader status --config "$CONFIG" --json | jq -e '
    .watcher == "running"
    and all(.services[]; .state == "running" and .health == "healthy")
    and ([.services[].name] | contains(["api","caddy","postgres","worker","worker-judge"]))'
}
doctor_ok() { reader doctor --config "$CONFIG" --json | jq -e 'all(.[]; .ok)'; }
check "all services healthy (four default plus kernels), watcher running" four_healthy_and_watcher
check "reader doctor: all ok"                  doctor_ok

# --- Edge -------------------------------------------------------------------------------------------------------
section "Edge (Caddy, ADR-0024)"
fetch_ca() { dc exec -T caddy cat /data/caddy/pki/authorities/local/root.crt >"$CACERT" && test -s "$CACERT"; }
check "Caddy local root fetched" fetch_ca
CURL=(curl -sS --cacert "$CACERT" --max-time 15)
healthz_ok() { "${CURL[@]}" -f "$READER_URL/api/healthz" | jq -e '.status == "ok"'; }
check "GET /api/healthz is ok"   healthz_ok
check "anonymous /api/me is 401" test "$("${CURL[@]}" -o /dev/null -w '%{http_code}' "$READER_URL/api/me")" = 401
headers_ok() { local h; h="$("${CURL[@]}" -D - -o /dev/null "$READER_URL/api/healthz" | tr -d '\r' | tr '[:upper:]' '[:lower:]')"
  grep -q '^strict-transport-security: max-age=31536000' <<<"$h" \
  && grep -q "^content-security-policy: default-src 'self';.*frame-ancestors 'none'" <<<"$h" \
  && grep -q '^x-content-type-options: nosniff' <<<"$h" \
  && grep -q '^referrer-policy: no-referrer' <<<"$h"; }
check "security headers and CSP" headers_ok
check "POST without Origin is refused (403)" test "$("${CURL[@]}" -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{}' "$READER_URL/api/auth/signin/options")" = 403

# --- Hardening and secrets (ADR-0026, FR-DEPLOY-9/10, INV-DEPLOY-2) -------------------------------------------------
section "Hardening and secrets (ADR-0026)"
check "postgres runs as 999 with a read-only root" \
  test "$(docker inspect -f '{{.Config.User}} {{.HostConfig.ReadonlyRootfs}}' "$(dc ps -q postgres)")" = "999:999 true"
servers_hardened() { local s; for s in api worker worker-judge; do
  [[ "$(docker inspect -f '{{.Config.User}} {{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}}' "$(dc ps -q "$s")")" == "10001:10001 true [ALL]" ]] || return 1
  done; }
check "api, worker and worker-judge run as 10001, read-only, cap-drop ALL" servers_hardened

# --- Judge lane (ADR-0039 B7b, OPS-DEPLOY FR-DEPLOY-16, INV-DEPLOY-11) ---------------------------------------------
lane_healthy() { [[ "$(health_of worker-judge)" == healthy ]]; }
check "worker-judge healthy" lane_healthy
lane_secrets_only_db_password() { [[ "$(dc exec -T worker-judge sh -c 'ls -A /run/secrets')" == db-password ]]; }
check "worker-judge /run/secrets holds only db-password" lane_secrets_only_db_password
# agentnet's IPAM and fixed addresses (FR-DEPLOY-16 c); an older ipam-less agentnet fails here: see the header note.
agentnet_ok() { local net; net="$(docker network inspect "${PROJECT}_agentnet")" || return 1
  jq -e '.[0].IPAM.Config[0].Subnet == "10.230.7.0/24" and .[0].IPAM.Config[0].IPRange == "10.230.7.128/25"' <<<"$net" >/dev/null || return 1
  local s want; for s in worker:10.230.7.10 worker-judge:10.230.7.11; do want="${s#*:}"
    [[ "$(docker inspect -f '{{(index .NetworkSettings.Networks "'"${PROJECT}_agentnet"'").IPAddress}}' "$(dc ps -q "${s%%:*}")")" == "$want" ]] || return 1
  done; }
check "agentnet 10.230.7.0/24 with worker .10 and worker-judge .11" agentnet_ok
proxy_on_agentnet_only() { dc ps -q agent-proxy | grep -q . || return 0   # agents off: nothing to check
  [[ "$(docker inspect -f '{{(index .NetworkSettings.Networks "'"${PROJECT}_agentnet"'").IPAddress}}' "$(dc ps -q agent-proxy)")" == 10.230.7.2 ]] || return 1
  # Inside agent-proxy, loopback 8080 refuses: it listens on its agentnet address only (FR-DEPLOY-16 d).
  ! dc exec -T agent-proxy python -I -c "import socket; socket.create_connection(('127.0.0.1', 8080), 2).close()" 2>/dev/null; }
check "agent-proxy at 10.230.7.2, nothing on 127.0.0.1:8080" proxy_on_agentnet_only
secret_files_ok() { local s uid; for s in $CONSUMERS; do
  uid="$(dc exec -T "$s" id -u)" || return 1
  [[ "$(dc exec -T "$s" stat -c '%a %u' /run/secrets/db-password)" == "600 $uid" ]] || return 1
  done; }
check "secret files 0600, owned by the service uid" secret_files_ok
no_secret_in_diff() { local c; for c in $(dc ps -q); do
  docker diff "$c" | grep -q '^A /run/secrets/.' && return 1; done; return 0; }
check "nothing below /run/secrets in docker diff" no_secret_in_diff
subchecks PY "$ROOT/e2e/live/lib/secret_scan.py" --project "$PROJECT"
# shellcheck disable=SC2016  # the $$ and "$f" expand inside the container's sh, on purpose
blobs_writable() { [[ "$(dc exec -T api sh -c 'f=/var/lib/reader/blobs/.stage1-$$; : >"$f" && stat -c %u "$f"; rm -f "$f"')" == 10001 ]]; }
check "blobs volume writable as 10001" blobs_writable

# --- Passkeys (SRV-AUTH, learner live-check) ----------------------------------------------------------------------
section "Passkeys (SRV-AUTH, software authenticator, learner live-check)"
subchecks PY "$ROOT/e2e/live/lib/passkey_check.py" --project "$PROJECT" --url "$READER_URL" --cacert "$CACERT"

# --- Watcher behaviour (ADR-0026; restarts the api and postgres) ----------------------------------------------------
section "Watcher (ADR-0026)"
redelivered_by_watcher() { dc restart api >/dev/null 2>&1 || return 1; healthy_with_file_within api 45; }
check "re-delivered by the watcher after an api restart" redelivered_by_watcher
fails_closed_without_watcher() {
  reader watcher stop >/dev/null 2>&1 || return 1
  dc restart api >/dev/null 2>&1 || return 1
  sleep 20
  local h; h="$(health_of api)"
  reader_up || return 1                                    # restores the watcher before judging
  [[ "$h" != healthy ]] && healthy_with_file_within api 45; }
check "fails closed without the watcher, then reader up restores" fails_closed_without_watcher
postgres_restarts_without_file() {
  reader watcher stop >/dev/null 2>&1 || return 1
  dc restart postgres >/dev/null 2>&1 || { reader_up; return 1; }
  local _ ok=1; for _ in $(seq 45); do [[ "$(health_of postgres)" == healthy ]] && { ok=0; break; }; sleep 2; done
  reader_up || return 1
  return "$ok"; }
check "postgres restarts without its secret file" postgres_restarts_without_file
check "stack healthy again" four_healthy_and_watcher
record_consumer_peaks() { local s p; for s in $CONSUMERS; do
  p="$(dc exec -T "$s" cat /sys/fs/cgroup/memory.peak)" || return 1
  printf '       %s memory.peak=%s bytes\n' "$s" "$p" >&2; done; }
section "Consumer memory (FR-DEPLOY-9, informational)"
record_consumer_peaks 2>&1 | sed 's/^/  /' || true

# --- Backup-restore drill (opt-in, DESTRUCTIVE) ---------------------------------------------------------------------
if ((DRILL)); then
  section "Backup-restore drill (--drill: deletes and recreates the pgdata and blobs volumes)"
  BACKUP="$WORK/backup"
  check "reader backup" reader backup --config "$CONFIG" "$BACKUP"
  backup_private() { [[ -n "$(find "$BACKUP" -type f)" && -z "$(find "$BACKUP" -type f ! -perm 600)" ]]; }
  check "backup files are 0600" backup_private
  fresh_stack() {
    reader down --config "$CONFIG" </dev/null >/dev/null 2>&1 || return 1
    docker volume rm "${PROJECT}_pgdata" "${PROJECT}_blobs" >/dev/null || return 1
    reader_up; }
  check "fresh stack (volumes recreated)" fresh_stack
  check "reader restore onto the fresh stack" reader restore --config "$CONFIG" "$BACKUP"
  check "stack healthy after restore" four_healthy_and_watcher
  restored_passkey() { [[ "$(dc exec -T postgres psql -U postgres -d postgres -At -c \
    "SELECT count(*) FROM webauthn_credentials c JOIN users u ON u.id = c.owner_id WHERE u.handle = 'live-check'")" -ge 1 ]]; }
  check "restored database holds this run's live-check passkey" restored_passkey
fi

echo
if ((LIVE_FAIL == 0)); then echo "STAGE 1: PASS"; else echo "STAGE 1: FAIL"; fi
cat <<'EOF'

Owner steps (manual, once):
  1. reader bootstrap --config <your reader.yaml>     # opens Safari; create the owner passkey (Apple Passwords)
  2. open https://localhost/api/me in Safari           # expect "role":"owner"
EOF
summary
