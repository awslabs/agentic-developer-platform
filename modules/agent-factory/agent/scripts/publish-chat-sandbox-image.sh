#!/usr/bin/env bash
set -euo pipefail
: "${ADP_SOURCE_SHA:?immutable source commit required}"
: "${REGISTRY:?target ECR registry required}"
: "${AWS_REGION:?region required}"
[[ "$ADP_SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]
[[ "$REGISTRY" =~ ^[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com$ ]]
TAG="sandbox-${ADP_SOURCE_SHA}"
IMAGE="${REGISTRY}/adp-chat-agent:${TAG}"
LOOKUP_ERROR="$(mktemp)"
PUSH_ERROR="$(mktemp)"
trap 'rm -f "$LOOKUP_ERROR" "$PUSH_ERROR"' EXIT
if ! DIGEST=$(aws ecr describe-images --repository-name adp-chat-agent --image-ids "imageTag=${TAG}" \
    --region "$AWS_REGION" --query 'imageDetails[0].imageDigest' --output text 2>"$LOOKUP_ERROR"); then
  grep -q ImageNotFoundException "$LOOKUP_ERROR" || { echo 'Sandbox image registry lookup failed' >&2; exit 1; }
  aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY"
  docker build --pull --no-cache --build-context adp-cli=modules/gateway/cli -f modules/agent-factory/agent/Dockerfile.sandbox -t "$IMAGE" modules/agent-factory
  IMAGE_ID=$(docker image inspect --format '{{.Id}}' "$IMAGE")
  bash modules/agent-factory/agent/scripts/chat-sandbox-probe-image.test.sh "$IMAGE_ID"
  if ! docker push "$IMAGE" >"$PUSH_ERROR" 2>&1; then
    cat "$PUSH_ERROR" >&2
    # Another build of this source can publish while our build/probes run.
    # Keep the immutable winner, and test its exact digest before accepting it.
    # Authentication, network and other push failures still fail the build.
    grep -Eq 'tag invalid:.*already exists|ImageTagAlreadyExistsException' "$PUSH_ERROR" || exit 1
    DIGEST=$(aws ecr describe-images --repository-name adp-chat-agent --image-ids "imageTag=${TAG}" \
      --region "$AWS_REGION" --query 'imageDetails[0].imageDigest' --output text)
    [[ "$DIGEST" =~ ^sha256:[a-f0-9]{64}$ ]] || { echo 'Concurrent sandbox image digest not verified' >&2; exit 1; }
    WINNER="${REGISTRY}/adp-chat-agent@${DIGEST}"
    docker pull "$WINNER"
    bash modules/agent-factory/agent/scripts/chat-sandbox-probe-image.test.sh "$WINNER"
  else
    cat "$PUSH_ERROR"
  fi
  DIGEST=$(aws ecr describe-images --repository-name adp-chat-agent --image-ids "imageTag=${TAG}"     --region "$AWS_REGION" --query 'imageDetails[0].imageDigest' --output text)
fi
[[ "$DIGEST" =~ ^sha256:[a-f0-9]{64}$ ]] || { echo 'Sandbox image digest not verified' >&2; exit 1; }
echo "Verified sandbox release: ${REGISTRY}/adp-chat-agent@${DIGEST}"
