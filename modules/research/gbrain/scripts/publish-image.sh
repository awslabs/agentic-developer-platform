#!/usr/bin/env bash
# Build once per source revision; immutable retries reuse the registry artifact.
set -euo pipefail
: "${REGISTRY:?REGISTRY is required}"
: "${AWS_REGION:?AWS_REGION is required}"
: "${ADP_SOURCE_SHA:?ADP_SOURCE_SHA is required}"
[[ "$ADP_SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo 'Expected a full source SHA' >&2; exit 1; }
GBRAIN_REPO=adp-research-gbrain
GBRAIN_IMAGE="$REGISTRY/$GBRAIN_REPO:$ADP_SOURCE_SHA"
GBRAIN_ERROR=$(mktemp)
trap 'rm -f "$GBRAIN_ERROR"' EXIT
if GBRAIN_DIGEST=$(aws ecr describe-images --region "$AWS_REGION" \
    --repository-name "$GBRAIN_REPO" --image-ids "imageTag=$ADP_SOURCE_SHA" \
    --query 'imageDetails[0].imageDigest' --output text 2>"$GBRAIN_ERROR"); then
  [[ "$GBRAIN_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo 'Registry returned no valid digest' >&2; exit 1; }
  echo "Reusing $REGISTRY/$GBRAIN_REPO@$GBRAIN_DIGEST"
  exit 0
elif ! grep -q 'ImageNotFoundException' "$GBRAIN_ERROR"; then
  cat "$GBRAIN_ERROR" >&2
  exit 1
fi
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY"
cd "$(dirname "${BASH_SOURCE[0]}")/.."
docker build --no-cache --pull -t "$GBRAIN_IMAGE" -f docker/Dockerfile .
# A failed build or push cannot reach the publication receipt below.
docker push "$GBRAIN_IMAGE"
GBRAIN_DIGEST=$(aws ecr describe-images --region "$AWS_REGION" \
  --repository-name "$GBRAIN_REPO" --image-ids "imageTag=$ADP_SOURCE_SHA" \
  --query 'imageDetails[0].imageDigest' --output text)
[[ "$GBRAIN_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo 'Published digest missing' >&2; exit 1; }
echo "Published $REGISTRY/$GBRAIN_REPO@$GBRAIN_DIGEST"
