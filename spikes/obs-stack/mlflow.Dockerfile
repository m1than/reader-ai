# Spike image (never shipped): pinned MLflow plus psycopg2 (the stock image has no Postgres driver: measured,
# `import psycopg2` fails in ghcr.io/mlflow/mlflow:v3.18.0) and a data directory owned by a non-root uid, so a fresh
# named volume is writable under read_only and cap_drop ALL (the same pattern as OPS-DEPLOY's blobs mount point).
FROM ghcr.io/mlflow/mlflow:v3.18.0
RUN pip install --no-cache-dir psycopg2-binary==2.9.11 && mkdir -p /var/lib/mlflow && chown 10004:10004 /var/lib/mlflow
USER 10004
WORKDIR /var/lib/mlflow
