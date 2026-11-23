#!/usr/bin/env bash
# Spike codemirror-bundle: gzip size of the lab editor's lazy chunk (WEB-NOTEBOOK FR-NOTEBOOK-9, NFR-NOTEBOOK-1).
# Installs the packages into a temporary directory, bundles editor.js with esbuild and prints raw and gzip (level 9).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cp "$here/editor.js" "$tmp/"
cd "$tmp"
npm init -y >/dev/null
npm install --silent --no-audit --no-fund @codemirror/state @codemirror/view @codemirror/commands \
  @codemirror/language @codemirror/lang-python @lezer/highlight esbuild >/dev/null
npm ls --depth=0
npx esbuild editor.js --bundle --minify --format=esm --outfile=out.js --log-level=warning
node -e "const z=require('zlib'),f=require('fs');const b=f.readFileSync('out.js');console.log('raw',b.length,'gzip9',z.gzipSync(b,{level:9}).length)"
