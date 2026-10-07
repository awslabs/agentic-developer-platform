#!/usr/bin/env bash
set -euo pipefail

image="${1:-}"
if [[ ! "$image" =~ ^(sha256:|[a-zA-Z0-9][a-zA-Z0-9./:_-]*@sha256:)[a-f0-9]{64}$ ]]; then
  printf '%s\n' 'Probe packaging blocked: supply an immutable local image ID or repository digest' >&2
  exit 3
fi
if ! command -v docker >/dev/null 2>&1; then
  printf '%s\n' 'Probe packaging blocked: an authorized Docker runtime is required' >&2
  exit 3
fi
if ! docker image inspect "$image" >/dev/null 2>&1; then
  printf '%s\n' 'Probe packaging blocked: the exact image must already exist in the authorized runtime' >&2
  exit 3
fi
docker run --rm --pull never --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --pids-limit 64 --memory 512m --cpus 1 \
  --tmpfs /tmp:rw,nosuid,nodev,size=16m \
  --workdir /tmp --entrypoint /bin/sh "$image" -eu -c '
  test "$(id -u)" = 10001
  for client in bash curl aws timeout node; do command -v "$client" >/dev/null; done
  aws --version
  curl --version
  node -e '\''
    const loadSdk = require("node:module").createRequire("/app/chat-sandbox-probe");
    for (const name of [
      "@aws-sdk/credential-provider-node", "@aws-sdk/client-sts",
      "@aws-sdk/client-s3", "@aws-sdk/client-dynamodb",
      "@aws-sdk/client-secrets-manager", "@aws-sdk/client-bedrock-runtime",
    ]) loadSdk(name);
    console.log("All isolation-probe SDK modules load");
  '\''
  test -x /app/chat-sandbox-entrypoint
  test -x /app/chat-sandbox-probe
  test ! -e /var/run/adp-model/token
  set +e
  ADP_PROBE_AUTHORIZED=true ADP_CHAT_DATA_ENABLED=true \
    ADP_WORKLOAD_TOKEN_FILE=/var/run/adp-model/token \
    /app/chat-sandbox-probe >/tmp/probe-output 2>&1
  result=$?
  set -e
  test "$result" -eq 3
  grep -q "Sandbox probe refused" /tmp/probe-output
  printf "%s\n" "Probe packaging passed; runtime isolation was not tested"
'
printf 'Verified packaging image: %s\n' "$image"
