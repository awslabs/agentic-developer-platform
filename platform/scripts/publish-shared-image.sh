#!/usr/bin/env bash
# Shared publication contract: source SHA only, no mutable aliases.
set -euo pipefail
: "${ADP_SOURCE_SHA:?ADP_SOURCE_SHA is required}"
: "${REGISTRY:?REGISTRY is required}"
: "${AWS_REGION:?AWS_REGION is required}"
[[ "$ADP_SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo 'Expected a full source SHA' >&2; exit 1; }
[[ "${IMAGE_TAG:-$ADP_SOURCE_SHA}" == "$ADP_SOURCE_SHA" ]] || { echo 'IMAGE_TAG must match archived source SHA' >&2; exit 1; }
[[ "${PUBLISH_LATEST:-false}" == false ]] || { echo 'Mutable latest publication is unsupported' >&2; exit 1; }
SHARED_BUILD_OPTIONS=(--no-cache --pull)
case "${PUBLISH_LOCAL_BUILD:-false}" in
  true) SHARED_BUILD_OPTIONS=() ;; # Preserve local Docker cache behavior.
  false) ;;
  *) echo 'PUBLISH_LOCAL_BUILD must be true or false' >&2; exit 1 ;;
esac
SHARED_REPO="${1:?Repository required}"
case "$SHARED_REPO" in
  adp-gateway|adp-agent-runtime|adp-chat-agent|adp-agent-gateway) ;;
  *) echo 'Unsupported shared repository' >&2; exit 1 ;;
esac
SHARED_ROOT="${PUBLISH_SOURCE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SHARED_ROOT="$(cd "$SHARED_ROOT" && pwd)"
SHARED_IMAGE="$REGISTRY/$SHARED_REPO:$ADP_SOURCE_SHA"
SHARED_ERROR=$(mktemp)
trap 'rm -f "$SHARED_ERROR"' EXIT
# Repositories are owned/provisioned by Terraform, never silently created here.
if SHARED_DIGEST=$(aws ecr describe-images --region "$AWS_REGION" \
    --repository-name "$SHARED_REPO" --image-ids "imageTag=$ADP_SOURCE_SHA" \
    --query 'imageDetails[0].imageDigest' --output text 2>"$SHARED_ERROR"); then
  [[ "$SHARED_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo 'Registry returned no valid digest' >&2; exit 1; }
  echo "Reusing $REGISTRY/$SHARED_REPO@$SHARED_DIGEST"
  exit 0
elif ! grep -q 'ImageNotFoundException' "$SHARED_ERROR"; then
  cat "$SHARED_ERROR" >&2
  exit 1
fi
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY"
cd "$SHARED_ROOT"
case "$SHARED_REPO" in
  adp-gateway)
    bash modules/gateway/scripts/stage-contracts.sh
    cd modules/gateway
    docker build "${SHARED_BUILD_OPTIONS[@]}" --build-arg "GATEWAY_RELEASE=$ADP_SOURCE_SHA" -t "$SHARED_IMAGE" .
    docker run --rm --entrypoint python "$SHARED_IMAGE" -m pricing_policy.selfcheck
    docker run --rm --entrypoint python "$SHARED_IMAGE" -m src.orchestration.review_contract_selfcheck
    docker run --rm --entrypoint python "$SHARED_IMAGE" -m src.orchestration.evaluation_contract_selfcheck
    ;;
  adp-agent-runtime)
    docker build "${SHARED_BUILD_OPTIONS[@]}" -f modules/agent-factory/agent-worker-image/Dockerfile -t "$SHARED_IMAGE" .
    docker run --rm --entrypoint python3 "$SHARED_IMAGE" -m lib.contract_selfcheck
    ;;
  adp-chat-agent)
    cd modules/agent-factory
    docker build "${SHARED_BUILD_OPTIONS[@]}" -f agent/Dockerfile -t "$SHARED_IMAGE" .
    ;;
  adp-agent-gateway)
    # Older immutable source revisions predate this build input. New revisions
    # must stage from their own archived canonical sources, never the checkout.
    if grep -Fq 'COPY security/stdlib/' modules/agent-factory/gateway/Dockerfile; then
      bash modules/agent-factory/scripts/stage-security-bundles.sh
    fi
    cd modules/agent-factory
    docker build "${SHARED_BUILD_OPTIONS[@]}" -f gateway/Dockerfile -t "$SHARED_IMAGE" .
    ;;
esac
docker push "$SHARED_IMAGE"
SHARED_DIGEST=$(aws ecr describe-images --region "$AWS_REGION" \
  --repository-name "$SHARED_REPO" --image-ids "imageTag=$ADP_SOURCE_SHA" \
  --query 'imageDetails[0].imageDigest' --output text)
[[ "$SHARED_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo 'Published digest missing' >&2; exit 1; }
echo "Published $REGISTRY/$SHARED_REPO@$SHARED_DIGEST"
