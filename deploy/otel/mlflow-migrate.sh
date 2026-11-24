#!/bin/sh
# mlflow-migrate (SRV-OBS FR-OBS-29, OPS-DEPLOY FR-DEPLOY-15): the one-shot that prepares MLflow's SQLite store
# before the server starts; compose's `mlflow` waits for it with `service_completed_successfully`, so the server
# never runs a schema migration itself (spikes/obs-stack round 9: a server stopped during its own first-start
# migration left the file unable to start). Runs in the reader-mlflow image under mlflow's hardening, with no
# network; the obs_mlflow volume at /var/lib/mlflow is its only writable path. Every step is under an exclusive
# flock, so two concurrent `reader up` runs cannot migrate twice.
#
# Files in the volume: store.version (the MLflow version that last migrated the store), backup/pre-migrate.db and
# backup/pre-migrate.version (the one pre-migration backup), backup/in-progress, backup/restored-for, backup/fresh.
# Exit codes (mlflow-migrate's own; `reader restore-obs-premigrate` uses 0..6):
#   10 obs_migration_incomplete       a previous migration of a store with data did not finish: restore first
#   11 obs_restored_for_other_version the store was restored for another MLflow version: revert the pin
#   12 obs_migration_failed           `mlflow db upgrade` (or the fresh store's initialisation) failed
#   13 obs_migration_finish_failed    WAL or store.version could not be written
# READER_MLFLOW_MIGRATE_FALLBACK=1 initialises a fresh store with MLflow's SqlAlchemyStore instead of
# `mlflow db upgrade` (the fallback for a pin whose `db upgrade` cannot build an empty store; spike round 13
# measured both on 3.18.0: 71 tables at head f4a9b2c7d1e6).
set -u

D=/var/lib/mlflow
DB="$D/mlflow.db"
B="$D/backup"
URI="sqlite:///$DB"
LEGACY_VERSION=3.18.0   # a store without store.version was made by MLflow 3.18.0's own start (declared)

log() { echo "mlflow-migrate: $*" >&2; }

mkdir -p "$B" || { log "cannot create $B"; exit 13; }
exec 9>"$D/.migrate.lock" || { log "cannot open the lock file"; exit 13; }
flock 9 || { log "cannot take the lock"; exit 13; }

VERSION=$(python -c 'from importlib.metadata import version; print(version("mlflow"))') || exit 13

sqlalchemy_store() {
  python -c "from mlflow.store.tracking.sqlalchemy_store import SqlAlchemyStore as S; S('$URI', '$D/artifacts')"
}

initialise() {
  # A missing (or wiped) store: the schema from empty, in this script, never in the server (H1). `db upgrade`
  # first; a pin whose `db upgrade` cannot build an empty file (3.10.0: "no such table: metrics", measured by
  # test_images_migrate) gets the fallback on a clean file. READER_MLFLOW_MIGRATE_FALLBACK=1 skips `db upgrade`.
  if [ "${READER_MLFLOW_MIGRATE_FALLBACK:-}" = 1 ]; then
    sqlalchemy_store
    return
  fi
  mlflow db upgrade "$URI" && return 0
  log "mlflow db upgrade could not build the store from empty: the SqlAlchemyStore fallback"
  rm -f "$DB" "$DB-wal" "$DB-shm" && sqlalchemy_store
}

finish() {
  # (4) WAL (persistent, FR-OBS-28), store.version, then the markers go; exit 0.
  python -c "import sqlite3; c = sqlite3.connect('$DB'); assert c.execute('pragma journal_mode=wal').fetchone()[0] == 'wal'" \
    || { log "obs_migration_finish_failed: WAL could not be set"; exit 13; }
  printf '%s\n' "$VERSION" > "$D/store.version.tmp" && mv "$D/store.version.tmp" "$D/store.version" \
    || { log "obs_migration_finish_failed: store.version could not be written"; exit 13; }
  rm -f "$B/in-progress" "$B/fresh" || exit 13
  log "store at MLflow $VERSION"
  exit 0
}

wipe() {
  rm -f "$DB" "$DB-wal" "$DB-shm" "$B/in-progress"
}

# (0) An interrupted first start of an empty volume: try to finish it; wipe only if that fails (nothing to lose).
if [ -f "$B/fresh" ]; then
  if [ -f "$DB" ] && initialise; then
    finish
  fi
  log "an interrupted fresh start: wiping the half-made store"
  wipe
elif [ -f "$B/in-progress" ]; then
  log "obs_migration_incomplete: a previous migration did not finish; run \`reader restore-obs-premigrate\`"
  exit 10
fi
if [ -f "$B/restored-for" ]; then
  restored=$(cat "$B/restored-for")
  if [ "$restored" != "$VERSION" ]; then
    log "obs_restored_for_other_version: restored for MLflow $restored, this image is $VERSION; revert the pin"
    exit 11
  fi
  rm -f "$B/restored-for" || exit 13
fi

# (1) A fresh volume, or a normal start.
if [ ! -f "$DB" ]; then
  : > "$B/fresh" || exit 13
  initialise || { log "obs_migration_failed: the fresh store could not be initialised"; exit 12; }
  finish
fi
if [ -f "$D/store.version" ] && [ "$(cat "$D/store.version")" = "$VERSION" ]; then
  finish   # no backup, no in-progress window
fi

# (2) The one pre-migration backup, through SQLite's backup API; never while a migration is unfinished.
from_version=$LEGACY_VERSION
if [ -f "$D/store.version" ]; then from_version=$(cat "$D/store.version"); fi
python -c "
import sqlite3
src = sqlite3.connect('file:$DB?mode=ro', uri=True)
dst = sqlite3.connect('$B/pre-migrate.db.tmp')
src.backup(dst)
dst.close(); src.close()
" || { rm -f "$B/pre-migrate.db.tmp"; log "obs_migration_failed: the pre-migration backup failed"; exit 12; }
mv "$B/pre-migrate.db.tmp" "$B/pre-migrate.db" || exit 12
printf '%s\n' "$from_version" > "$B/pre-migrate.version" || exit 12

# (3) The migration, marked in progress until (4) finishes.
: > "$B/in-progress" || exit 12
mlflow db upgrade "$URI" || { log "obs_migration_failed: mlflow db upgrade failed; run \`reader restore-obs-premigrate\`"; exit 12; }

finish
