#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../../../.." && pwd)"
case "${ADP_CONTROL_FIXTURE_MODE:-}" in
  registered-control|native-interrupt) ;;
  *) echo 'An authenticated registered fixture mode is required' >&2; exit 2 ;;
esac
: "${ADP_CONTROL_TOKEN:?Production registration is required}"
: "${ADP_CONTROL_FIXTURE_OUTPUT:?A private output path is required}"
cd "$repo_root/modules/agent-factory/agent"
npm ci --include=dev --ignore-scripts
if [[ "${ADP_CONTROL_RETRY_EVAL:-}" == "true" ]]; then
  exec node node_modules/ts-node/dist/bin.js src/control-retry.integration.ts
fi
exec node node_modules/ts-node/dist/bin.js src/registered-control-fixture.ts
