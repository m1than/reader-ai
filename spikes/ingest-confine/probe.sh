#!/usr/bin/env bash
# Spike ingest-confine: run probes.py inside the converter profile. Confined only: unconfined, some probes
# would really read files in the owner's home.
#   bash spikes/ingest-confine/probe.sh
set -euo pipefail
mode=confined
here=$(cd "$(dirname "$0")" && pwd -P)
root=$(cd "$here/../.." && pwd -P)
data=$root/data/spikes/ingest-confine
venv=$data/venv
pyroot=$(cd "$(dirname "$(readlink -f "$venv/bin/python")")/.." && pwd -P)
work=$data/work-probe-$mode
rm -rf "$work"; mkdir -p "$work"; chmod 700 "$work"
outside=$data/outside-$mode.txt
rm -f "$outside"
cmd=(sandbox-exec -f "$here/converter.sb" -D PYROOT="$pyroot" -D VENV="$venv" -D MODELS="$data/models"
     -D SCRIPTS="$here" -D WORK="$work" "$venv/bin/python" -I "$here/probes.py" "$work" "$outside")
cd "$work"
env -i PATH=/usr/bin:/bin HOME="$work" TMPDIR="$work/" LANG=en_US.UTF-8 "${cmd[@]}"
