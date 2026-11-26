#!/usr/bin/env bash
# Entry point of the throwaway pgvector container (run by run.sh as user postgres, --network none, PGDATA on
# tmpfs): init a cluster, run pg.sql, stop. No server outlives the container; nothing is published.
set -euo pipefail
export PGDATA=/tmp/pg
initdb -A trust -E UTF8 --locale=C.UTF-8 >/dev/null
pg_ctl -w -o "-c listen_addresses= -c unix_socket_directories=/tmp -c maintenance_work_mem=512MB" \
  -l /tmp/pg.log start >/dev/null
psql -h /tmp -q -X -v ON_ERROR_STOP=1 -d postgres -f /w/pg.sql
pg_ctl -w stop >/dev/null
