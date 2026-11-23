# reader-mlflow:local (SRV-OBS FR-OBS-8): the pinned MLflow image plus a data directory owned by the non-root uid,
# so a fresh named volume is writable under read_only and cap_drop ALL (measured in spikes/obs-stack). Nothing else:
# the store is SQLite (ADR-0032), so no database driver is added.
FROM ghcr.io/mlflow/mlflow:v3.18.0@sha256:257204c77980b0f3450a26eb6089e487603fcdd4509529eadaf88f90669f5c13
RUN mkdir -p /var/lib/mlflow && chown 10004:10004 /var/lib/mlflow
USER 10004
