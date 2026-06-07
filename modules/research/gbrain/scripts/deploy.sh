#!/usr/bin/env bash
# =============================================================================
# deploy.sh — End-to-end deploy for gbrain experimental module
# Usage: ./scripts/deploy.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE_DIR="$(dirname "$SCRIPT_DIR")"
TF_DIR="${MODULE_DIR}/terraform"
DOCKER_DIR="${MODULE_DIR}/docker"

AWS_REGION="${AWS_REGION:-us-east-1}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
STATE_BUCKET="adp-terraform-state-${ACCOUNT_ID}"
IMAGE_NAME="adp-research-gbrain"
IMAGE_TAG="${IMAGE_TAG:-latest}"

echo "=== gbrain Deployment ==="
echo "Account:  ${ACCOUNT_ID}"
echo "Region:   ${AWS_REGION}"
echo "Registry: ${REGISTRY}"
echo ""

# Step 1: Create ECR repository (idempotent)
echo "--- Step 1: Ensuring ECR repository exists..."
aws ecr describe-repositories --repository-names "${IMAGE_NAME}" --region "${AWS_REGION}" 2>/dev/null \
  || aws ecr create-repository --repository-name "${IMAGE_NAME}" --region "${AWS_REGION}" --image-tag-mutability MUTABLE

# Step 2: Build Docker image
echo "--- Step 2: Building Docker image..."
cd "${MODULE_DIR}"
docker build -t "${IMAGE_NAME}:${IMAGE_TAG}" -f docker/Dockerfile .

# Step 3: Push to ECR
echo "--- Step 3: Pushing to ECR..."
aws ecr get-login-password --region "${AWS_REGION}" | docker login --username AWS --password-stdin "${REGISTRY}"
docker tag "${IMAGE_NAME}:${IMAGE_TAG}" "${REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG}"
docker push "${REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG}"
echo "Pushed: ${REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG}"

# Step 4: Terraform init + apply
echo "--- Step 4: Terraform apply..."
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
  -auto-approve

echo ""
echo "=== Deployment Complete ==="
terraform output -json
