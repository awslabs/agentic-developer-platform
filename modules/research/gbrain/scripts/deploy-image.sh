#!/usr/bin/env bash
# Provision only build prerequisites before publication; activate ECS by digest.
# Rollback: GBRAIN_IMAGE_DIGEST=sha256:<previous digest> bash scripts/deploy-image.sh
set -euo pipefail
GBRAIN_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GBRAIN_REPO_ROOT="$(cd "$GBRAIN_SCRIPT_DIR/../../../.." && pwd)"
AWS_REGION="${AWS_REGION:-us-east-1}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
STATE_BUCKET="adp-terraform-state-${ACCOUNT_ID}"
GBRAIN_DIGEST="${GBRAIN_IMAGE_DIGEST:-}"
if [ -n "$GBRAIN_DIGEST" ]; then
  [[ "$GBRAIN_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo 'Invalid rollback digest' >&2; exit 1; }
else
  SOURCE_SHA="${SOURCE_SHA:-$(git -C "$GBRAIN_REPO_ROOT" rev-parse HEAD)}"
  [[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo 'Expected a full source SHA' >&2; exit 1; }
fi
cd "$GBRAIN_SCRIPT_DIR/../terraform"
terraform init -backend-config="bucket=$STATE_BUCKET" \
  -backend-config=key=research/gbrain/terraform.tfstate \
  -backend-config="region=$AWS_REGION" -backend-config=encrypt=true \
  -backend-config=dynamodb_table=adp-terraform-locks -input=false -reconfigure
if [ -z "$GBRAIN_DIGEST" ]; then
  # Deliberate bootstrap exception: these targets include no ECS task/service.
  # The full apply below is allowed only after publication and registry verification.
  terraform apply -var-file=environments/dev.tfvars -var="state_bucket=$STATE_BUCKET" \
    -target=module.storage -target=module.build -input=false -auto-approve
  PROJECT=$(terraform output -raw build_project_name)
  ADP_RELEASE_BUILD=true STATE_BUCKET="$STATE_BUCKET" AWS_REGION="$AWS_REGION" SOURCE_SHA="$SOURCE_SHA" \
    bash "$GBRAIN_REPO_ROOT/platform/scripts/codebuild-run.sh" "$PROJECT"
  GBRAIN_DIGEST=$(aws ecr describe-images --region "$AWS_REGION" \
    --repository-name adp-research-gbrain --image-ids "imageTag=$SOURCE_SHA" \
    --query 'imageDetails[0].imageDigest' --output text)
  [[ "$GBRAIN_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo 'Build did not publish a valid digest' >&2; exit 1; }
fi
# Resolve by digest even on rollback. Missing/unreadable artifacts stop before activation.
GBRAIN_VERIFIED=$(aws ecr describe-images --region "$AWS_REGION" \
  --repository-name adp-research-gbrain --image-ids "imageDigest=$GBRAIN_DIGEST" \
  --query 'imageDetails[0].imageDigest' --output text)
[[ "$GBRAIN_VERIFIED" == "$GBRAIN_DIGEST" ]] || { echo 'Digest verification failed' >&2; exit 1; }
terraform apply -var-file=environments/dev.tfvars -var="state_bucket=$STATE_BUCKET" \
  -var="container_image_digest=$GBRAIN_DIGEST" -input=false -auto-approve
