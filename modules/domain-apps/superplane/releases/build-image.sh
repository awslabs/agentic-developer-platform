#!/usr/bin/env bash
# Builds only the selected Superplane component from verified, staged source.
set -euo pipefail
component="${1:?component required}"
case "$component" in
  superplane-api|superplane-controller|superplane-platform-monitor) ;;
  *) echo "Unknown Superplane component" >&2; exit 1 ;;
esac
[[ "${UPSTREAM_REVISION:-}" =~ ^[0-9a-f]{40}$ ]] || { echo "Invalid upstream revision" >&2; exit 1; }
[[ "${UPSTREAM_PATH:-}" == "src/$component" ]] || { echo "Invalid source scope" >&2; exit 1; }
[[ "${ECR_REPO:-}" == "adp-$component" ]] || { echo "Invalid ECR scope" >&2; exit 1; }
[[ "${ACCOUNT_ID:-}" =~ ^[0-9]{12}$ ]] || { echo "Invalid target account" >&2; exit 1; }
[[ "${AWS_REGION:-}" =~ ^[a-z]{2}(-[a-z]+)+-[0-9]+$ ]] || { echo "Invalid region" >&2; exit 1; }
expected_registry="$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
[[ "${REGISTRY:-}" == "$expected_registry" ]] || { echo "Registry/account mismatch" >&2; exit 1; }
source_root="modules/domain-apps/superplane/releases/source"
[[ -f "$source_root/.superplane-revision" ]] || { echo "Verified source is not staged" >&2; exit 1; }
[[ "$(cat "$source_root/.superplane-revision")" == "$UPSTREAM_REVISION" ]] || { echo "Staged source revision mismatch" >&2; exit 1; }
context="$source_root/$UPSTREAM_PATH"
[[ -f "$context/Dockerfile" && ! -L "$context" ]] || { echo "Component Dockerfile missing" >&2; exit 1; }
# The future acquisition step verifies the actual source against the lock before
# writing this marker. The marker alone is not proof of artifact authenticity.
aws ecr describe-repositories --repository-names "$ECR_REPO" --region "$AWS_REGION" >/dev/null
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY"
tag="$REGISTRY/$ECR_REPO:$UPSTREAM_REVISION"
docker build --label "org.opencontainers.image.revision=$UPSTREAM_REVISION" -t "$tag" "$context"
docker push "$tag"
aws ecr describe-images --repository-name "$ECR_REPO" --image-ids "imageTag=$UPSTREAM_REVISION" --region "$AWS_REGION" --query 'imageDetails[0].imageDigest' --output text
