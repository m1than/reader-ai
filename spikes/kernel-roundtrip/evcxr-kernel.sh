#!/bin/sh
# Rust kernel launcher (spike). evcxr's compile cache is not safe to share between concurrent kernels,
# and its rustc wrapper writes to the cache even on hits. So every kernel gets its own work dir in the
# exec tmpfs (/run/evcxr) with a private copy of the read-only pre-built cache. COLD=1: empty cache.
set -eu
dir=$(mktemp -d /run/evcxr/kernel.XXXXXX)
if [ -z "${COLD:-}" ]; then cp -r /opt/evcxr-cache "$dir/cache"; else mkdir -p "$dir/cache"; fi
export XDG_CACHE_HOME="$dir/cache" TMPDIR="$dir"
exec evcxr_jupyter "$@"
