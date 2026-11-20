#!/bin/sh
# reader-postgres entrypoint (OPS-DEPLOY FR-DEPLOY-10 d, ADR-0026). The cluster password is set only at initdb, so
# the wrapper waits for the delivered /run/secrets/db-password and passes POSTGRES_PASSWORD_FILE only while PGDATA
# is empty. On later starts the variable stays unset, so a restart without `reader up` still starts the database
# (the stage-1 live check verifies that the official entrypoint reads the variable on every start).
set -eu
# As PID 1 the shell ignores SIGTERM without a handler; exit at once while waiting (the exec below replaces it).
trap 'exit 143' TERM
if [ ! -s "$PGDATA/PG_VERSION" ]; then
  while [ ! -s /run/secrets/db-password ]; do
    sleep 1
  done
  export POSTGRES_PASSWORD_FILE=/run/secrets/db-password
else
  unset POSTGRES_PASSWORD_FILE
fi
exec docker-entrypoint.sh "$@"
