#!/usr/bin/env bash
# =============================================================================
# deploy.sh — End-to-end deploy for gbrain experimental module
# Usage: ./scripts/deploy.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE_DIR="$(dirname "$SCRIPT_DIR")"
TF_DIR="${MODULE_DIR}/terraform"

AWS_REGION="${AWS_REGION:-us-east-1}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
STATE_BUCKET="adp-terraform-state-${ACCOUNT_ID}"

echo "=== gbrain Deployment ==="
echo "Account:  ${ACCOUNT_ID}"
echo "Region:   ${AWS_REGION}"
echo ""

# Step 1: Terraform init + apply (creates all infra including the CodeBuild project)
echo "--- Step 1: Terraform apply..."
cd "${TF_DIR}"

terraform init \
  -backend-config="bucket=${STATE_BUCKET}" \
  -backend-config="key=research/gbrain/terraform.tfstate" \
  -backend-config="region=${AWS_REGION}" \
  -backend-config="encrypt=true" \
  -backend-config="dynamodb_table=adp-terraform-locks" \
  -input=false \
  -reconfigure

terraform apply \
  -var-file=environments/dev.tfvars \
  -var="state_bucket=${STATE_BUCKET}" \
  -auto-approve

# Step 2: Trigger CodeBuild to build and push the container image
echo "--- Step 2: Building container image via CodeBuild..."
PROJECT=$(terraform output -raw build_project_name)
echo "CodeBuild project: ${PROJECT}"

BUILD_ID=$(aws codebuild start-build \
  --project-name "${PROJECT}" \
  --region "${AWS_REGION}" \
  --query 'build.id' --output text)
echo "Build started: ${BUILD_ID}"

# Step 3: Poll until build completes
echo "--- Step 3: Waiting for build to complete..."
while true; do
  STATUS=$(aws codebuild batch-get-builds \
    --ids "${BUILD_ID}" \
    --region "${AWS_REGION}" \
    --query 'builds[0].buildStatus' --output text)

  case "${STATUS}" in
    IN_PROGRESS)
      echo "  Build in progress..."
      sleep 15
      ;;
    SUCCEEDED)
      echo "  Build succeeded!"
      break
      ;;
    *)
      echo "  Build failed with status: ${STATUS}"
      echo "  Fetching logs URL..."
      aws codebuild batch-get-builds \
        --ids "${BUILD_ID}" \
        --region "${AWS_REGION}" \
        --query 'builds[0].logs.deepLink' --output text
      exit 1
      ;;
  esac
done

echo ""
echo "=== Deployment Complete ==="
terraform output -json
