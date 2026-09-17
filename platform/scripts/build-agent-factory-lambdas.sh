#!/usr/bin/env bash
# Build all factory Lambda artifacts before Terraform plans their source hashes.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
if [ -n "${ADP_RELEASE_DIR:-}" ]; then
  python3 "$SCRIPT_DIR/release/artifacts.py" verify-prepared --directory "$ADP_RELEASE_DIR"
  exit 0
fi

bash "$SCRIPT_DIR/build-ingest-lambda.sh"
cd "$ROOT_DIR/modules/agent-factory/agent"
npm ci --include=dev --no-audit --no-fund
OUTPUT="$ROOT_DIR/modules/agent-factory/infra/.build/session-sweeper/index.js"
mkdir -p "$(dirname "$OUTPUT")"
./node_modules/.bin/esbuild src/complex-task-chat/sweepers/session-sweeper.ts \
  --bundle --platform=node --target=node22 --format=cjs --outfile="$OUTPUT"
node -e 'const handler = require(process.argv[1]).handler; if (typeof handler !== "function") throw new Error("Missing sweeper handler"); handler({Records: []}).catch(error => { console.error(error); process.exit(1); });' "$OUTPUT"
echo "Factory Lambda packages built; session-sweeper handler loaded successfully"
