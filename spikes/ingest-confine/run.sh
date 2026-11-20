#!/usr/bin/env bash
# Spike ingest-confine runner.
#   bash spikes/ingest-confine/run.sh <pdf> <bare|confined> <tag> [pages]
# Needs data/spikes/ingest-confine/{venv,models} (see RESULT.md "Setup"). Prints the converter's JSON line
# (numbers only) and, for confined runs, the sandbox denials logged during the run (operation and path only).
set -euo pipefail
pdf=$1 mode=$2 tag=$3
pages=${4:-128,130,132,133,141,143,378,381,461,468,483,497,559,592,602,661,672,700,776,812}
here=$(cd "$(dirname "$0")" && pwd -P)
root=$(cd "$here/../.." && pwd -P)
data=$root/data/spikes/ingest-confine
venv=$data/venv
pyroot=$(cd "$(dirname "$(readlink -f "$venv/bin/python")")/.." && pwd -P)
work=$data/work-$tag
rm -rf "$work"; mkdir -p "$work/out"; chmod 700 "$work"
cp "$pdf" "$work/in.pdf"
cmd=("$venv/bin/python" -I "$here/convert_confined.py" "$work/in.pdf" "$pages" "$work/out" "$data/models")
start=$(date '+%Y-%m-%d %H:%M:%S')
if [ "$mode" = confined ]; then
  cmd=(sandbox-exec -f "$here/converter.sb" -D PYROOT="$pyroot" -D VENV="$venv" -D MODELS="$data/models"
       -D SCRIPTS="$here" -D WORK="$work" "${cmd[@]}")
fi
cd "$work"   # the converter's cwd must be readable inside the profile (os.getcwd)
set +e
env -i PATH=/usr/bin:/bin HOME="$work" TMPDIR="$work/" LANG=en_US.UTF-8 "${cmd[@]}" >"$work/result.json" 2>"$work/stderr.txt"
rc=$?
set -e
echo "rc=$rc"
cat "$work/result.json"
if [ "$mode" = confined ]; then
  sleep 2
  /usr/bin/log show --start "$start" --style compact --predicate 'subsystem == "com.apple.sandbox.reporting" OR sender == "Sandbox"' 2>/dev/null \
    | grep 'Sandbox: python3' | grep -o 'deny([0-9]*) [a-z*-]* [^ ",]*' | sort | uniq -c | sort -rn | head -40
fi
