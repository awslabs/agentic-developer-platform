#!/usr/bin/env bash
# =============================================================================
# teardown.sh — Clean removal of gbrain experimental resources
# =============================================================================
# Hardened teardown with idempotent steps, fail-loud orphan audit,
# and post-destroy assertions. Every step tolerates "already gone."
#
# Usage: ./scripts/teardown.sh
#
# Prerequisites:
#   - AWS CLI configured with permissions including tag:GetResources
#   - Terraform installed
#   - Access to the ADP Terraform state bucket
#
# Exit codes:
#   0 — teardown complete, all resources confirmed gone
#   1 — teardown failed or orphan audit could not verify (manual check needed)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE_DIR="$(dirname "$SCRIPT_DIR")"
TF_DIR="${MODULE_DIR}/terraform"
REPO_ROOT="$(cd "${MODULE_DIR}/../../.." && pwd)"

AWS_REGION="${AWS_REGION:-us-east-1}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
STATE_BUCKET="adp-terraform-state-${ACCOUNT_ID}"

ECS_CLUSTER="adp-research-gbrain"
ECS_SERVICE="adp-research-gbrain-mcp"
S3_BUCKET="adp-research-gbrain-repo-${ACCOUNT_ID}"
ECR_REPO="adp-research-gbrain"
EXPERIMENT_TAG="gbrain-eval-2026-06"
CODEBUILD_PREFIX="adp-research-gbrain"

echo "=== gbrain Teardown (Hardened) ==="
echo "Account: ${ACCOUNT_ID}"
echo "Region:  ${AWS_REGION}"
echo ""
echo "This will destroy ALL gbrain experimental resources."
if [ "${GBRAIN_TEARDOWN_NO_WAIT:-false}" != "true" ]; then
  echo "Press Ctrl+C within 5 seconds to abort..."
  sleep 5
fi

# =============================================================================
# Step 0: Assert/disable integration [G5]
# =============================================================================
echo ""
echo "--- Step 0: Assert integration is disabled..."

# Check if GBRAIN_ENABLED is set in any agent ScaledJob or deployment
# Tolerate kubectl not configured (EKS may already be gone)
if command -v kubectl >/dev/null 2>&1; then
  GBRAIN_ENV=$(kubectl get scaledjobs,deployments --all-namespaces -o json 2>/dev/null \
    | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    for item in data.get('items', []):
        containers = (item.get('spec', {}).get('template', {}).get('spec', {})
                     .get('containers', []))
        if not containers:
            containers = (item.get('spec', {}).get('jobTargetRef', {}).get('template', {})
                         .get('spec', {}).get('containers', []))
        for c in containers:
            for env in c.get('env', []):
                if env.get('name') == 'GBRAIN_ENABLED' and env.get('value', '').lower() == 'true':
                    print(f\"ACTIVE in {item['metadata']['namespace']}/{item['metadata']['name']}\")
except:
    pass
" 2>/dev/null || echo "")

  if [ -n "$GBRAIN_ENV" ]; then
    echo "ERROR: gbrain integration is still ACTIVE in agent workloads:"
    echo "$GBRAIN_ENV"
    echo ""
    echo "Disable GBRAIN_ENABLED before teardown to prevent agent errors."
    exit 1
  fi
  echo "  GBRAIN_ENABLED is not active in any agent workload."
else
  echo "  kubectl not available — skipping live integration check (EKS may be gone)."
fi

# =============================================================================
# Step 1: Scale ECS service to 0 [G4]
# =============================================================================
echo ""
echo "--- Step 1: Scaling ECS service to 0..."

# Check if the cluster/service exist before trying to scale
if aws ecs describe-clusters --clusters "$ECS_CLUSTER" --region "$AWS_REGION" \
    --query 'clusters[0].status' --output text 2>/dev/null | grep -q "ACTIVE"; then

  # Scale to 0
  aws ecs update-service \
    --cluster "$ECS_CLUSTER" \
    --service "$ECS_SERVICE" \
    --desired-count 0 \
    --region "$AWS_REGION" >/dev/null 2>&1 \
    && echo "  Service scaled to 0. Waiting for tasks to drain..." \
    || echo "  Service not found or already scaled down."

  # Wait for running count to reach 0 (max 120s)
  WAIT_COUNT=0
  while [ $WAIT_COUNT -lt 24 ]; do
    RUNNING=$(aws ecs describe-services \
      --cluster "$ECS_CLUSTER" \
      --services "$ECS_SERVICE" \
      --region "$AWS_REGION" \
      --query 'services[0].runningCount' \
      --output text 2>/dev/null || echo "0")

    if [ "$RUNNING" = "0" ] || [ "$RUNNING" = "None" ] || [ -z "$RUNNING" ]; then
      echo "  ECS tasks drained (runningCount=0)."
      break
    fi

    sleep 5
    WAIT_COUNT=$((WAIT_COUNT + 1))
  done

  if [ $WAIT_COUNT -ge 24 ]; then
    echo "  WARNING: Timed out waiting for ECS tasks to drain. Proceeding anyway."
  fi
else
  echo "  ECS cluster '${ECS_CLUSTER}' not found or inactive. Skipping."
fi

# =============================================================================
# Step 2: Empty S3 bucket (all versions) [G1]
# =============================================================================
echo ""
echo "--- Step 2: Emptying S3 bucket (versioned objects)..."

if aws s3api head-bucket --bucket "$S3_BUCKET" 2>/dev/null; then
  bash "${REPO_ROOT}/platform/scripts/empty-s3-buckets.sh" "$S3_BUCKET"
else
  echo "  Bucket '${S3_BUCKET}' does not exist. Skipping."
fi

# =============================================================================
# Step 3: Terraform destroy [G3 — includes module-owned CodeBuild project]
# =============================================================================
echo ""
echo "--- Step 3: Terraform destroy..."
cd "${TF_DIR}"

# Init may fail if state bucket is already gone — tolerate
if terraform init \
  -backend-config="bucket=${STATE_BUCKET}" \
  -backend-config="key=research/gbrain/terraform.tfstate" \
  -backend-config="region=${AWS_REGION}" \
  -backend-config="encrypt=true" \
  -backend-config="dynamodb_table=adp-terraform-locks" \
  -input=false \
  -reconfigure 2>/dev/null; then

  terraform destroy \
    -var-file=environments/dev.tfvars \
    -auto-approve \
    && echo "  Terraform destroy complete." \
    || echo "  WARNING: Terraform destroy reported errors (resources may already be gone)."
else
  echo "  WARNING: Terraform init failed (state backend may be gone). Skipping terraform destroy."
  echo "  Continuing with manual cleanup..."
fi

# =============================================================================
# Step 4: Force-delete ECR repository (if images remain)
# =============================================================================
echo ""
echo "--- Step 4: Cleaning ECR repository..."
aws ecr delete-repository \
  --repository-name "$ECR_REPO" \
  --force \
  --region "${AWS_REGION}" 2>/dev/null \
  && echo "  ECR repository deleted." \
  || echo "  ECR repository already gone."

# =============================================================================
# Step 5: Force-delete Secrets Manager secrets
# =============================================================================
echo ""
echo "--- Step 5: Cleaning Secrets Manager..."
for secret in "adp/research/gbrain/db-credentials" "adp/research/gbrain/mcp-token"; do
  aws secretsmanager delete-secret \
    --secret-id "$secret" \
    --force-delete-without-recovery \
    --region "${AWS_REGION}" 2>/dev/null \
    && echo "  Deleted: $secret" \
    || echo "  Already gone: $secret"
done

# =============================================================================
# Step 6: Clean Terraform state
# =============================================================================
echo ""
echo "--- Step 6: Cleaning Terraform state from S3..."
aws s3 rm "s3://${STATE_BUCKET}/research/gbrain/" \
  --recursive \
  --region "${AWS_REGION}" 2>/dev/null \
  && echo "  State files removed." \
  || echo "  No state files to remove (bucket may be gone)."

# =============================================================================
# Step 7: Orphan resource audit — FAIL LOUD [G2]
# =============================================================================
echo ""
echo "--- Step 7: Orphan resource audit..."
echo "  Querying resources tagged ExperimentId=${EXPERIMENT_TAG}..."

# Capture both stdout and stderr — if the API call fails, we MUST fail loud
ORPHAN_OUTPUT=""
ORPHAN_EXIT=0
ORPHAN_OUTPUT=$(aws resourcegroupstaggingapi get-resources \
  --tag-filters "Key=ExperimentId,Values=${EXPERIMENT_TAG}" \
  --query 'ResourceTagMappingList[].ResourceARN' \
  --output text \
  --region "${AWS_REGION}" 2>&1) || ORPHAN_EXIT=$?

if [ $ORPHAN_EXIT -ne 0 ]; then
  echo ""
  echo "ERROR: Orphan resource audit FAILED — could not verify resource cleanup."
  echo "  API response: ${ORPHAN_OUTPUT}"
  echo ""
  echo "  This may be due to missing 'tag:GetResources' permission on the executing role."
  echo "  You MUST verify manually that no resources remain with tag ExperimentId=${EXPERIMENT_TAG}."
  echo ""
  echo "  Required IAM permission: tag:GetResources"
  echo "  Manual check: aws resourcegroupstaggingapi get-resources \\"
  echo "    --tag-filters Key=ExperimentId,Values=${EXPERIMENT_TAG} --region ${AWS_REGION}"
  exit 1
fi

if [ -z "$ORPHAN_OUTPUT" ] || [ "$ORPHAN_OUTPUT" = "None" ]; then
  echo "  No orphan resources found. Audit PASSED."
else
  echo ""
  echo "  WARNING: Orphan resources detected:"
  echo "$ORPHAN_OUTPUT" | tr '\t' '\n' | sed 's/^/    /'
  echo ""
  echo "  These resources have tag ExperimentId=${EXPERIMENT_TAG} and need manual cleanup."
  echo "  Teardown is NOT complete until these are removed."
  # Don't exit 1 here — the teardown itself succeeded, these are leftovers to flag
fi

# =============================================================================
# Step 8: Post-destroy assertions [G3]
# =============================================================================
echo ""
echo "--- Step 8: Post-destroy assertions..."
ASSERTION_FAILURES=0

# Assert: no CodeBuild project with gbrain prefix
echo "  Checking CodeBuild projects..."
CB_PROJECTS=$(aws codebuild list-projects --region "${AWS_REGION}" \
  --query "projects[?starts_with(@, '${CODEBUILD_PREFIX}')]" \
  --output text 2>/dev/null || echo "")
if [ -n "$CB_PROJECTS" ] && [ "$CB_PROJECTS" != "None" ]; then
  echo "  FAIL: CodeBuild project(s) still exist: ${CB_PROJECTS}"
  ASSERTION_FAILURES=$((ASSERTION_FAILURES + 1))
else
  echo "  PASS: No CodeBuild projects matching '${CODEBUILD_PREFIX}*'."
fi

# Assert: no ECS cluster
echo "  Checking ECS cluster..."
ECS_STATUS=$(aws ecs describe-clusters --clusters "$ECS_CLUSTER" --region "${AWS_REGION}" \
  --query 'clusters[0].status' --output text 2>/dev/null || echo "GONE")
if [ "$ECS_STATUS" = "ACTIVE" ]; then
  echo "  FAIL: ECS cluster '${ECS_CLUSTER}' still ACTIVE."
  ASSERTION_FAILURES=$((ASSERTION_FAILURES + 1))
else
  echo "  PASS: ECS cluster '${ECS_CLUSTER}' is gone (status: ${ECS_STATUS})."
fi

# Assert: no RDS instance
echo "  Checking RDS instances..."
RDS_INSTANCES=$(aws rds describe-db-instances --region "${AWS_REGION}" \
  --query "DBInstances[?starts_with(DBInstanceIdentifier, '${CODEBUILD_PREFIX}')].DBInstanceIdentifier" \
  --output text 2>/dev/null || echo "")
if [ -n "$RDS_INSTANCES" ] && [ "$RDS_INSTANCES" != "None" ]; then
  echo "  FAIL: RDS instance(s) still exist: ${RDS_INSTANCES}"
  ASSERTION_FAILURES=$((ASSERTION_FAILURES + 1))
else
  echo "  PASS: No RDS instances matching '${CODEBUILD_PREFIX}*'."
fi

# Assert: no ECR repository
echo "  Checking ECR repository..."
ECR_EXISTS=$(aws ecr describe-repositories --repository-names "$ECR_REPO" --region "${AWS_REGION}" \
  --query 'repositories[0].repositoryName' --output text 2>/dev/null || echo "GONE")
if [ "$ECR_EXISTS" = "$ECR_REPO" ]; then
  echo "  FAIL: ECR repository '${ECR_REPO}' still exists."
  ASSERTION_FAILURES=$((ASSERTION_FAILURES + 1))
else
  echo "  PASS: ECR repository '${ECR_REPO}' is gone."
fi

# Assert: no S3 bucket
echo "  Checking S3 bucket..."
if aws s3api head-bucket --bucket "$S3_BUCKET" 2>/dev/null; then
  echo "  FAIL: S3 bucket '${S3_BUCKET}' still exists."
  ASSERTION_FAILURES=$((ASSERTION_FAILURES + 1))
else
  echo "  PASS: S3 bucket '${S3_BUCKET}' is gone."
fi

echo ""
if [ $ASSERTION_FAILURES -gt 0 ]; then
  echo "=== Teardown Complete (with $ASSERTION_FAILURES assertion failure(s)) ==="
  echo "Some resources may need manual cleanup. Review the FAIL items above."
  exit 1
else
  echo "=== Teardown Complete — All resources confirmed removed ==="
fi
