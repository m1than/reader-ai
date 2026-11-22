#!/bin/sh
# Generator of deploy/images/embed.requirements.lock (OPS-DEPLOY FR-DEPLOY-2, SRV-SEARCH FR-SEARCH-2, ADR-0028).
# Resolves the embed service's runtime for the reader-embed image (python:3.14-slim-trixie, linux/arm64) with a
# sha256 per artifact; torch comes from the PyTorch CPU index (2.15.0+cpu). Run from the repository root:
#   sh deploy/images/embed.lock.sh
set -eu
cd "$(dirname "$0")"
printf '%s\n' 'torch==2.15.0' 'transformers==5.23.0' 'sentence-transformers==6.3.0' |
  uv pip compile - \
    --python-version 3.14 \
    --python-platform aarch64-manylinux_2_28 \
    --generate-hashes \
    --index-url https://download.pytorch.org/whl/cpu \
    --extra-index-url https://pypi.org/simple \
    --index-strategy unsafe-best-match \
    --no-header \
    --output-file embed.requirements.lock
