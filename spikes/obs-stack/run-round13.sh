#!/usr/bin/env bash
# Spike obs-stack, round 13 (design review round 6, H1): how mlflow-migrate can initialise an empty store itself,
# measured on the pinned image with no network:
#   A. `mlflow db upgrade` on a missing file (FR-OBS-29's fresh-volume hypothesis);
#   B. one Python call that constructs MLflow's SqlAlchemyStore on a missing file (the fallback);
#   then, for each: the alembic head, the table count, a second `mlflow db upgrade` (a no-op), and a server start
#   (health 200). Zero credit; volumes are removed at exit.
#   bash spikes/obs-stack/run-round13.sh
set -euo pipefail
cd "$(dirname "$0")"
OUT="$PWD/results/$(date +%Y%m%dT%H%M%S)-round13-init"
mkdir -p "$OUT"
IMG=ghcr.io/mlflow/mlflow:v3.18.0@sha256:257204c77980b0f3450a26eb6089e487603fcdd4509529eadaf88f90669f5c13
trap 'docker volume rm r13-a r13-b >/dev/null 2>&1 || true' EXIT
INSPECT='import sqlite3, json; c = sqlite3.connect("/v/mlflow.db"); t = [r[0] for r in c.execute("select name from sqlite_master where type=\"table\"")]; h = c.execute("select version_num from alembic_version").fetchall() if "alembic_version" in t else None; print(json.dumps({"tables": len(t), "alembic": h}))'
SERVE='(mlflow server --host 127.0.0.1 --port 5000 --backend-store-uri sqlite:////v/mlflow.db --artifacts-destination /v/art >/v/server.log 2>&1 &); for i in $(seq 60); do python -c "import urllib.request; print(urllib.request.urlopen(\"http://127.0.0.1:5000/health\", timeout=2).status)" 2>/dev/null && break; sleep 1; done'
for case in a b; do
  docker volume create "r13-$case" >/dev/null
  if [ "$case" = a ]; then
    INIT='mlflow db upgrade sqlite:////v/mlflow.db'
  else
    INIT='python -c "from mlflow.store.tracking.sqlalchemy_store import SqlAlchemyStore as S; S(\"sqlite:////v/mlflow.db\", \"/v/art\")"'
  fi
  docker run --rm --network none -v "r13-$case:/v" --entrypoint sh "$IMG" -c "
    set +e
    echo '--- init'; $INIT >/v/init.log 2>&1; echo \"init exit \$?\"; tail -2 /v/init.log
    echo '--- after init'; python -c '$INSPECT'
    echo '--- second db upgrade'; mlflow db upgrade sqlite:////v/mlflow.db >/v/up2.log 2>&1; echo \"upgrade exit \$?\"
    python -c '$INSPECT'
    echo '--- server'; $SERVE
  " >"$OUT/case-$case.txt" 2>&1 || true
done
cat "$OUT"/case-*.txt
echo "results in $OUT"
