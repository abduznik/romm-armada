#!/usr/bin/env bash
# Renders the panel in jsdom with stand-ins for Decky's components and a scripted backend (test/ui/), and runs
# the scenarios with node's test runner. Needs `pnpm install` once.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
OUT="$ROOT/build/ui-test"
mkdir -p "$OUT"
./node_modules/.bin/esbuild test/ui/ui.test.tsx --bundle --platform=node --format=esm --jsx=automatic --log-level=warning \
    --alias:@decky/api="$ROOT/test/ui/stubs/api.ts" --alias:@decky/ui="$ROOT/test/ui/stubs/ui.tsx" \
    --external:jsdom --external:node:* --outfile="$OUT/ui.test.mjs" --banner:js="import { createRequire as __cr } from 'node:module'; const require = __cr(import.meta.url);"
cd "$OUT"
ln -sfn "$ROOT/node_modules" node_modules
node --test --test-force-exit --test-timeout=30000 --test-reporter=spec ui.test.mjs
