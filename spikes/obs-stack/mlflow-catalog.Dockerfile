# Spike image (never shipped), round 5 (review B1): the pinned MLflow with its bundled model catalog replaced by one
# file, catalog/anthropic.json (every bundled Anthropic model plus pricing.yaml's ids, all at 0; gen_catalog.py).
# No mount and no MLFLOW_MODEL_CATALOG_URI: the provider-less bundled scan then finds only zero prices.
FROM ghcr.io/mlflow/mlflow:v3.18.0@sha256:257204c77980b0f3450a26eb6089e487603fcdd4509529eadaf88f90669f5c13
COPY catalog/anthropic.json /tmp/reader-catalog.json
RUN P=/usr/local/lib/python3.11/site-packages/mlflow/utils/model_catalog \
    && rm -f "$P"/*.json && cp /tmp/reader-catalog.json "$P/anthropic.json" && rm /tmp/reader-catalog.json \
    && mkdir -p /var/lib/mlflow && chown 10004:10004 /var/lib/mlflow
USER 10004
WORKDIR /var/lib/mlflow
