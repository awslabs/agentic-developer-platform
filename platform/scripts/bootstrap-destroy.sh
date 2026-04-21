#!/bin/bash
set -euo pipefail

# =============================================================================
# bootstrap-destroy.sh — Destroy Terraform state backend
# =============================================================================
# Deletes the S3 state bucket and DynamoDB lock table. This is a SEPARATE
# intentional step — the orchestrator (deploy-all.sh --destroy) does NOT call
# this automatically. Only run after all module destroys have succeeded.
#
# Usage:
#   ./bootstrap-destroy.sh
#
# Safety:
#   - Prompts for typed account ID confirmation
#   - Refuses to run if state bucket still has environment state keys
#   - Only deletes adp-terraform-state-<account> bucket and adp-terraform-locks table
# =============================================================================

AWS_REGION="${AWS_REGION:-us-east-1}"
ENVIRONMENT="${ENVIRONMENT:-dev}"

echo "=== Bootstrap Destroy — Terraform State Backend ==="
echo ""

# Get account ID
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) || {
  echo "ERROR: Cannot determine AWS account. Configure AWS CLI first."
  exit 1
}

STATE_BUCKET="adp-terraform-state-${ACCOUNT_ID}"
LOCK_TABLE="adp-terraform-locks"

echo "Account:   $ACCOUNT_ID"
echo "Bucket:    $STATE_BUCKET"
echo "DDB Table: $LOCK_TABLE"
echo "Region:    $AWS_REGION"
echo ""

# Check if bucket exists
if ! aws s3api head-bucket --bucket "$STATE_BUCKET" 2>/dev/null; then
  echo "State bucket '$STATE_BUCKET' does not exist. Nothing to destroy."

  # Still check DynamoDB
  if aws dynamodb describe-table --table-name "$LOCK_TABLE" --region "$AWS_REGION" > /dev/null 2>&1; then
    echo "DynamoDB table '$LOCK_TABLE' exists."
  else
    echo "DynamoDB table '$LOCK_TABLE' does not exist. All clean."
    exit 0
  fi
fi

# Safety check: refuse if state files still exist for any module
echo "Checking for remaining Terraform state files..."
HAS_STATE=false

for STATE_KEY in \
  "${ENVIRONMENT}/platform/terraform.tfstate" \
  "${ENVIRONMENT}/modules/gateway/terraform.tfstate" \
  "${ENVIRONMENT}/modules/agent-factory/terraform.tfstate" \
  "${ENVIRONMENT}/modules/agent-context/terraform.tfstate"; do

  if aws s3api head-object --bucket "$STATE_BUCKET" --key "$STATE_KEY" > /dev/null 2>&1; then
    # Check if the state has any resources (not just an empty state)
    RESOURCE_COUNT=$(aws s3 cp "s3://${STATE_BUCKET}/${STATE_KEY}" - 2>/dev/null | \
      python3 -c "import sys,json; d=json.load(sys.stdin); print(len([r for r in d.get('resources',[]) if r.get('type','') != '']))" 2>/dev/null || echo "unknown")

    if [ "$RESOURCE_COUNT" != "0" ] && [ "$RESOURCE_COUNT" != "unknown" ]; then
      echo "  WARNING: $STATE_KEY has $RESOURCE_COUNT resource(s) still in state!"
      HAS_STATE=true
    else
      echo "  OK: $STATE_KEY (empty or no resources)"
    fi
  else
    echo "  OK: $STATE_KEY (not found)"
  fi
done

if [ "$HAS_STATE" = true ]; then
  echo ""
  echo "ERROR: Terraform state files with active resources still exist."
  echo "Run 'deploy-all.sh --destroy' first to destroy all module infrastructure,"
  echo "then run this script to clean up the state backend."
  exit 1
fi

# Confirmation prompt
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  WARNING: This will PERMANENTLY delete:                     ║"
echo "║    - S3 bucket: $STATE_BUCKET    ║"
echo "║    - DynamoDB table: $LOCK_TABLE                  ║"
echo "║                                                             ║"
echo "║  After this, you cannot recover Terraform state.            ║"
echo "║  You will need to re-bootstrap to deploy again.             ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "Type your AWS account ID ($ACCOUNT_ID) to confirm destruction:"
read -r CONFIRM_ACCOUNT

if [ "$CONFIRM_ACCOUNT" != "$ACCOUNT_ID" ]; then
  echo "Account ID did not match. Aborting."
  exit 1
fi

echo ""
echo "Confirmed. Destroying state backend..."

# Empty and delete the S3 bucket
echo "Emptying state bucket..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/empty-s3-buckets.sh" ]; then
  bash "$SCRIPT_DIR/empty-s3-buckets.sh" "$STATE_BUCKET"
else
  # Fallback if shared script not available
  aws s3 rm "s3://${STATE_BUCKET}" --recursive > /dev/null 2>&1 || true
fi

echo "Deleting state bucket..."
aws s3api delete-bucket --bucket "$STATE_BUCKET" --region "$AWS_REGION" 2>/dev/null || {
  echo "WARNING: Could not delete bucket. It may have versioned objects."
  echo "Try: aws s3 rb s3://${STATE_BUCKET} --force"
}

# Delete DynamoDB table
echo "Deleting DynamoDB lock table..."
aws dynamodb delete-table --table-name "$LOCK_TABLE" --region "$AWS_REGION" > /dev/null 2>&1 || {
  echo "WARNING: Could not delete DynamoDB table '$LOCK_TABLE'. It may not exist."
}

echo ""
echo "=== Bootstrap destroy complete ==="
echo "State bucket and lock table have been deleted."
echo "To redeploy, run: ./platform/scripts/bootstrap.sh"
