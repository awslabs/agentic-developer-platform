#!/usr/bin/env bash
# Publish the optional hosted Cyber layer after hosted-integration Terraform
# creates adp-cyber-hosted-worker in this AWS account.
set -euo pipefail
: "${CYBER_WORKER_BASE_IMAGE:?Set the reviewed base worker image at an immutable digest}"
[[ "$CYBER_WORKER_BASE_IMAGE" =~ @sha256:[0-9a-f]{64}$ ]] || { echo 'Base worker image must use an OCI digest' >&2; exit 2; }
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
AWS_REGION="${AWS_REGION:-us-east-1}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
SOURCE_SHA="$(git -C "$ROOT_DIR" rev-parse HEAD)"
REPO=adp-cyber-hosted-worker
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
aws ecr describe-repositories --region "$AWS_REGION" --repository-names "$REPO" >/dev/null
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY"
cd "$ROOT_DIR"
docker build --pull -f modules/domain-apps/cyber/agent/Dockerfile.hosted-worker \
  --build-arg "WORKER_BASE_IMAGE=$CYBER_WORKER_BASE_IMAGE" \
  -t "$REGISTRY/$REPO:$SOURCE_SHA" .
docker run --rm --entrypoint python3 "$REGISTRY/$REPO:$SOURCE_SHA" -m lib.contract_selfcheck
docker run --rm --entrypoint python3 "$REGISTRY/$REPO:$SOURCE_SHA" -c 'import cyber_tools.task_report; assert __import__("os").path.isfile("/app/task-agents/cyber/dist/driver.mjs")'
docker push "$REGISTRY/$REPO:$SOURCE_SHA"
DIGEST="$(aws ecr describe-images --region "$AWS_REGION" --repository-name "$REPO" --image-ids "imageTag=$SOURCE_SHA" --query 'imageDetails[0].imageDigest' --output text)"
[[ "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo 'Published Cyber image digest missing' >&2; exit 1; }
printf 'Cyber hosted worker: %s/%s@%s\nworker_image_digest = "%s"\n' "$REGISTRY" "$REPO" "$DIGEST" "$DIGEST"
