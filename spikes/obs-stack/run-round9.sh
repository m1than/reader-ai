#!/usr/bin/env bash
# Spike obs-stack, round 9: MLflow operations and load (SRV-OBS phase 2, FR-OBS-26..). Fresh spike stacks with the
# production flags under test (compose.round9.yaml); zero credit; own project; no published port; internal networks
# only; every stack is stopped at exit.
#   1. concurrency with SQLite's default journal, then with WAL (a fresh volume each);
#   2. ops on the WAL stack: disabled features, CORS and Host checks, health probes, online backup and restore,
#      VACUUM after a retention delete (MLflow stopped);
#   3. load on a fresh WAL stack: a whole book, 100 tutor questions, a bench upload, with `docker stats` sampled;
#   4. upgrade: a volume written by an older MLflow, started by 3.18.0 without and with `mlflow db upgrade`.
#   bash spikes/obs-stack/run-round9.sh
set -euo pipefail
cd "$(dirname "$0")"
OUT="$PWD/results/$(date +%Y%m%dT%H%M%S)-round9-ops"
mkdir -p "$OUT"
export OUT BACKEND=sqlite CSP_CANDIDATE="default-src 'self'" CSP_JAEGER="default-src 'self'" JAEGER_MAX_TRACES=450
DC=(docker compose -p reader-spike-obs -f compose.yaml -f compose.catalog.yaml -f compose.jaeger.yaml
    -f compose.phase2.yaml -f compose.round9.yaml)
P=(--profile sqlite --profile jaeger --profile driver)
down() { "${DC[@]}" "${P[@]}" down -v --remove-orphans >/dev/null 2>&1 || true; }
trap 'down; docker rm -f r9-old r9-new >/dev/null 2>&1 || true; docker volume rm r9-upgrade >/dev/null 2>&1 || true' EXIT
drive() { "${DC[@]}" "${P[@]}" run --rm -e MODE="$1" -e SUFFIX="${2:-}" --entrypoint python driver2 /spike/round9.py \
            >>"$OUT/driver.stdout" 2>>"$OUT/driver.stderr"; }
up() { "${DC[@]}" --profile sqlite --profile jaeger up -d >/dev/null 2>&1; }
stopml() { "${DC[@]}" stop mlflow-sqlite >/dev/null 2>&1; }
startml() { "${DC[@]}" start mlflow-sqlite >/dev/null 2>&1; }

STEPS="${STEPS:-1234}"
"${DC[@]}" "${P[@]}" build -q
# Run 20261124T151501 measured that stopping MLflow during its first start's schema migration leaves the SQLite
# file half-migrated ("table _alembic_tmp_experiments already exists": MLflow never starts again). So every stop
# below first waits for /health (MODE=wait), i.e. for the migration to finish.
if [[ $STEPS == *1* ]]; then
  # 1. concurrency: default journal, then WAL.
  down; up; drive conc -delete
  down; up; drive wait; stopml; drive wal; startml; drive conc -wal
fi
if [[ $STEPS == *2* ]]; then
  # 2. ops on the WAL stack.
  drive ops
  stopml; drive vacuum; startml
fi
if [[ $STEPS == *3* ]]; then
  # 3. load on a fresh WAL stack, with docker stats every 5 s.
  down; up; drive wait; stopml; drive wal; startml
  ( while true; do docker stats --no-stream --format '{{.Name}} {{.CPUPerc}} {{.MemUsage}}' | grep reader-spike-obs \
      | sed "s/^/$(date +%s) /" >>"$OUT/stats-load.txt"; sleep 5; done ) &
  SAMPLER=$!
  drive load
  kill "$SAMPLER" || true
  down
fi
[[ $STEPS == *4* ]] || exit 0
# 4. upgrade from an older MLflow on the same volume.
OLD="${OLD_MLFLOW:-ghcr.io/mlflow/mlflow:v3.10.0}"
docker volume create r9-upgrade >/dev/null
docker run --rm -v r9-upgrade:/v --entrypoint sh "$OLD" -c 'mkdir -p /v && chmod 0777 /v' >/dev/null 2>&1 || true
docker run -d --name r9-old --network none -v r9-upgrade:/v --entrypoint sh "$OLD" -c \
  'mlflow server --host 127.0.0.1 --port 5000 --backend-store-uri sqlite:////v/mlflow.db --artifacts-destination /v/a & sleep 40; python -c "import urllib.request,json; r=urllib.request.Request(\"http://127.0.0.1:5000/api/2.0/mlflow/experiments/create\", data=json.dumps({\"name\":\"old\"}).encode(), headers={\"Content-Type\":\"application/json\"}); print(urllib.request.urlopen(r).read().decode())"; sleep 2' >/dev/null
docker wait r9-old >"$OUT/upgrade-old.exit"; docker logs r9-old >"$OUT/upgrade-old.log" 2>&1 || true
NEW=reader-spike-mlflow:local
docker run -d --name r9-new --network none -v r9-upgrade:/v --user 0 --entrypoint sh "$NEW" -c \
  'timeout 60 mlflow server --host 127.0.0.1 --port 5000 --backend-store-uri sqlite:////v/mlflow.db --artifacts-destination /v/a > /v/no-upgrade.log 2>&1; echo "no-upgrade exit $?" >> /v/no-upgrade.log; mlflow db upgrade sqlite:////v/mlflow.db > /v/upgrade.log 2>&1; echo "upgrade exit $?" >> /v/upgrade.log; (mlflow server --host 127.0.0.1 --port 5000 --backend-store-uri sqlite:////v/mlflow.db --artifacts-destination /v/a > /v/after.log 2>&1 &) ; sleep 30; python -c "import urllib.request; print(urllib.request.urlopen(\"http://127.0.0.1:5000/health\").status); print(urllib.request.urlopen(\"http://127.0.0.1:5000/api/2.0/mlflow/experiments/get-by-name?experiment_name=old\").read().decode()[:200])" >> /v/after.log 2>&1' >/dev/null
docker wait r9-new >/dev/null
docker run --rm -v r9-upgrade:/v --entrypoint sh "$NEW" -c 'tail -5 /v/no-upgrade.log; echo ---; tail -5 /v/upgrade.log; echo ---; tail -4 /v/after.log' >"$OUT/upgrade.txt" 2>&1 || true
echo "results in $OUT"
