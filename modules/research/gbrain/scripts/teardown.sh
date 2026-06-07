#!/usr/bin/env bash
# =============================================================================
# teardown.sh — Clean removal of gbrain experimental resources
# Usage: ./scripts/teardown.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE_DIR="$(dirname "$SCRIPT_DIR")"
TF_DIR="${MODULE_DIR}/terraform"

AWS_REGION="${AWS_REGION:-us-east-1}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
STATE_BUCKET="adp-terraform-state-${ACCOUNT_ID}"

echo "=== gbrain Teardown ==="
echo "Account: ${ACCOUNT_ID}"
echo ""
echo "This will destroy ALL gbrain experimental resources."
echo "Press Ctrl+C within 5 seconds to abort..."
sleep 5

# Step 1: Terraform destroy
echo "--- Step 1: Terraform destroy..."
cd "${TF_DIR}"

terraform init \
  -backend-config="bucket=${STATE_BUCKET}" \
  -backend-config="key=research/gbrain/terraform.tfstate" \
  -backend-config="region=${AWS_REGION}" \
  -backend-config="encrypt=true" \
  -backend-config="dynamodb_table=adp-terraform-locks" \
  -input=false \
  -reconfigure

terraform destroy \
  -var-file=environments/dev.tfvars \
  -auto-approve

# Step 2: Clean ECR (force-delete if images remain)
echo "--- Step 2: Cleaning ECR repository..."
aws ecr delete-repository --repository-name adp-research-gbrain --force --region "${AWS_REGION}" 2>/dev/null \
  && echo "ECR repository deleted" \
  || echo "ECR repository already gone"

# Step 3: Clean Secrets Manager (force-delete, no recovery window)
echo "--- Step 3: Cleaning Secrets Manager..."
for secret in "adp/research/gbrain/db-credentials" "adp/research/gbrain/mcp-token"; do
  aws secretsmanager delete-secret --secret-id "$secret" --force-delete-without-recovery --region "${AWS_REGION}" 2>/dev/null \
    && echo "Deleted: $secret" \
    || echo "Already gone: $secret"
done

# Step 4: Clean Terraform state
echo "--- Step 4: Cleaning Terraform state..."
aws s3 rm "s3://${STATE_BUCKET}/research/gbrain/" --recursive --region "${AWS_REGION}" 2>/dev/null \
  && echo "State files removed" \
  || echo "No state files to remove"

# Step 5: Orphan check
echo "--- Step 5: Orphan resource check..."
ORPHANS=$(aws resourcegroupstaggingapi get-resources \
  --tag-filters Key=ExperimentId,Values=gbrain-eval-2026-06 \
  --query 'ResourceTagMappingList[].ResourceARN' \
  --output text \
  --region "${AWS_REGION}" 2>/dev/null || echo "")

if [ -z "$ORPHANS" ] || [ "$ORPHANS" = "None" ]; then
  echo "No orphan resources found."
else
  echo "WARNING: Orphan resources detected:"
  echo "$ORPHANS"
  echo ""
  echo "These may need manual cleanup."
fi

echo ""
echo "=== Teardown Complete ==="
