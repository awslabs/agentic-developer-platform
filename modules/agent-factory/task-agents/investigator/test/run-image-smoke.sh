#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <worker-image-reference>" >&2
  exit 2
fi

command -v docker >/dev/null 2>&1 || {
  echo "docker is required for the built-image evidence lane" >&2
  exit 2
}

IMAGE=$1
ROOT=$(git rev-parse --show-toplevel)
SOURCE_SHA=$(git rev-parse HEAD)
FIXTURE_HASH=$(sha256sum "$ROOT/docs/task-api/contracts/v1/fixtures/valid/process-start-frame.json" | awk '{print $1}')
IMAGE_DIGEST=$(docker image inspect --format '{{index .RepoDigests 0}}' "$IMAGE" 2>/dev/null || true)
if [[ -z "$IMAGE_DIGEST" || "$IMAGE_DIGEST" == '<no value>' ]]; then
  IMAGE_DIGEST=$(docker image inspect --format '{{.Id}}' "$IMAGE")
fi

docker run --rm \
  --network none \
  --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=16m \
  --mount "type=bind,src=$ROOT,dst=/fixture,readonly" \
  --env "SOURCE_SHA=$SOURCE_SHA" \
  --env "FIXTURE_HASH=$FIXTURE_HASH" \
  --env "IMAGE_DIGEST=$IMAGE_DIGEST" \
  --entrypoint node \
  "$IMAGE" \
  /fixture/modules/agent-factory/task-agents/investigator/test/image-smoke.mjs
