#!/usr/bin/env bash
set -euo pipefail

script="$(dirname "$0")/chat-sandbox-probe.sh"
bash -n "$script"
sed -n '/^const denied =/,/^NODE$/p' "$script" | sed '$d' | node --check -

output="$(mktemp)"
trap 'rm -f "$output"' EXIT
if ADP_PROBE_AUTHORIZED=true ADP_CHAT_DATA_ENABLED=true ADP_WORKLOAD_TOKEN_FILE=/var/run/adp-model/token bash "$script" >"$output" 2>&1; then
  printf '%s\n' 'Probe must refuse without a real sandbox identity' >&2
  exit 1
fi
if ! grep -q 'Sandbox probe refused' "$output"; then
  printf '%s\n' 'Probe did not fail closed before cloud calls' >&2
  exit 1
fi
printf '%s\n' 'Sandbox probe preflight: pass (no AWS or network calls)'
