#!/usr/bin/env bash
# Generates sandbox/requirements.lock from sandbox/requirements.in: the full dependency closure, pinned, with
# sha256 hashes, for CPython 3.14 on linux/arm64 (OPS-SANDBOX FR-SANDBOX-1). The image installs it with
# `pip install --require-hashes --no-deps`.
set -euo pipefail
cd "$(dirname "$0")"
uv pip compile requirements.in \
  --generate-hashes \
  --python-version 3.14 \
  --python-platform aarch64-unknown-linux-gnu \
  --no-annotate \
  --custom-compile-command "bash sandbox/lock.sh" \
  --output-file requirements.lock
