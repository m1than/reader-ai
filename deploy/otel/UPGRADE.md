# Bumping an observability pin (SRV-OBS FR-OBS-34)

A pin of MLflow (`deploy/otel/mlflow.Dockerfile`'s `FROM`), Rotel or Jaeger (`deploy/compose.yaml`) changes only in
a PR whose description carries this checklist, each item ticked with its evidence:

1. The new image digest is pinned, and the release notes between the old and the new version were read for breaking
   changes (schema migrations, removed flags, changed environment variables, new default-on features).
2. `python deploy/otel/mlflow_catalog.py --check` passes. For a new MLflow, regenerate the bundled catalog list
   first, so the replaced catalog still prices every bundled model at 0 (FR-OBS-12 c).
3. The CLI canary (INV-OBS-2) passes: `READER_TEST_IMAGE_BUILD=1 uv run pytest tests/obs/test_canary.py`.
4. Every obs [image] test passes (`READER_TEST_IMAGE_BUILD=1 uv run pytest tests/obs -k images`), including:
   - FR-OBS-29's upgrade test from the previous pin (`test_images_migrate.py`; set its `N_IMAGE` to the old pin
     by digest);
   - FR-OBS-19's Jaeger CSP hashes.
5. The replay of VEC-OBS-5 `writes` against the new image (`test_images_phase2.py`) passes.
6. FR-OBS-35's burst test runs (`READER_TEST_BURST=1`), and its numbers are recorded in docs/progress.yaml.

The [config] test `test_compose_and_test_pins_agree` checks that the Dockerfile's base pin is the one the image
tests build on.
