#!/bin/sh
# Entrypoint wrapper for secret consumers (OPS-DEPLOY FR-DEPLOY-10 d, ADR-0026): waits until `reader up` has
# delivered /run/secrets/<name> (a tmpfs, lost on every restart), then runs the command.
#   wait-for-secret <name> -- <command> [args…]
set -eu
# As PID 1 the shell ignores SIGTERM without a handler; exit at once so `docker stop` needs no 10 s kill.
trap 'exit 143' TERM
name="$1"
shift
[ "${1:-}" = "--" ] && shift
while [ ! -s "/run/secrets/$name" ]; do
  sleep 1
done
exec "$@"
