#!/usr/bin/env bash
# Publish Agent Mail from an exact Git archive. Stdout contains only its digest URI.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
REGION=${AWS_REGION:-us-east-1}
REPOSITORY=mcp-agent-mail
SOURCE_SHA=${ADP_SOURCE_SHA:-$(git -C "$ROOT" rev-parse HEAD)}
DRY_RUN=false
for arg in "$@"; do
  case "$arg" in --dry-run) DRY_RUN=true;; *) echo "Unknown option: $arg" >&2; exit 1;; esac
done
[[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo 'Expected full ADP_SOURCE_SHA' >&2; exit 1; }
[[ ${IMAGE_TAG:-$SOURCE_SHA} == "$SOURCE_SHA" && ${PUBLISH_LATEST:-false} == false ]] || {
  echo 'Only the full source SHA tag is supported' >&2; exit 1;
}
git -C "$ROOT" cat-file -e "$SOURCE_SHA^{commit}"
CONTEXT=modules/harness/mcp-hub/docker/agent-mail
git -C "$ROOT" cat-file -e "$SOURCE_SHA:$CONTEXT/Dockerfile"
if $DRY_RUN; then
  echo "Would archive $SOURCE_SHA:$CONTEXT and publish $REPOSITORY:$SOURCE_SHA to an immutable ECR repository" >&2
  exit 0
fi
: "${ECR_REGISTRY:?Set ECR_REGISTRY explicitly}"
[[ "$ECR_REGISTRY" =~ ^([0-9]{12})\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com$ ]] || { echo 'Invalid ECR registry' >&2; exit 1; }
ACCOUNT=${BASH_REMATCH[1]}
[[ ${BASH_REMATCH[2]} == "$REGION" ]] || { echo 'ECR registry region mismatch' >&2; exit 1; }
MUTABILITY=$(aws ecr describe-repositories --registry-id "$ACCOUNT" --region "$REGION" \
  --repository-names "$REPOSITORY" --query 'repositories[0].imageTagMutability' --output text)
[[ "$MUTABILITY" == IMMUTABLE ]] || { echo 'Provision an IMMUTABLE ECR repository before publication' >&2; exit 1; }
STAGING=$(mktemp -d)
trap 'rm -rf "$STAGING"' EXIT
lookup() {
  aws ecr describe-images --registry-id "$ACCOUNT" --region "$REGION" \
    --repository-name "$REPOSITORY" --image-ids "imageTag=$SOURCE_SHA" \
    --query 'imageDetails[0].imageDigest' --output text
}
if DIGEST=$(lookup 2>"$STAGING/lookup-error"); then
  : # A published immutable source tag is reused; never overwrite it.
elif grep -q 'ImageNotFoundException' "$STAGING/lookup-error"; then
  git -C "$ROOT" archive "$SOURCE_SHA" "$CONTEXT" | tar -x -C "$STAGING"
  IMAGE="$ECR_REGISTRY/$REPOSITORY:$SOURCE_SHA"
  aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$ECR_REGISTRY" >&2
  docker build --pull --no-cache --label "org.opencontainers.image.revision=$SOURCE_SHA" \
    -t "$IMAGE" -f "$STAGING/$CONTEXT/Dockerfile" "$STAGING/$CONTEXT" >&2
  docker push "$IMAGE" >&2
  DIGEST=$(lookup)
else
  echo 'Cannot resolve ECR source tag; refusing publication' >&2
  exit 1
fi
[[ "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo 'ECR returned no valid image digest' >&2; exit 1; }
printf '%s/%s@%s\n' "$ECR_REGISTRY" "$REPOSITORY" "$DIGEST"
