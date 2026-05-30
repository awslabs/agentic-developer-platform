#!/usr/bin/env bash
# =============================================================================
# One-shot migration: move KEDA helm_release + IAM role from agent-factory
# (Phase 8) terraform state to webhook-ingress (Phase 7) terraform state.
# =============================================================================
# Run AFTER the refactor PR (#1052) merges but BEFORE the next
# agent-factory-infra-apply or webhook-ingress-deploy workflow run.
#
# KEDA stays installed throughout — this is a state-only move.
#
# Usage:
#   ./platform/scripts/migrate-keda-ownership.sh [--dry-run]
#
# Verification:
#   On an already-migrated account, --dry-run should be a clean no-op:
#     ./platform/scripts/migrate-keda-ownership.sh --dry-run
#   Expected output: "No KEDA resources found in source state."
#
# Prerequisites:
#   - AWS credentials configured (same account where KEDA is deployed)
#   - terraform CLI available
#   - Both modules already initialized (terraform init has been run)
#
# Affected accounts (as of 2026-05-30):
#   - Platform: 879318057152
#   - Customer: 403685770643
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SRC_DIR="${REPO_ROOT}/modules/agent-factory/infra"
DST_DIR="${REPO_ROOT}/modules/agent-factory/webhook-ingress/infra"

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
  echo "[DRY RUN] Will show what would be migrated without making changes."
fi

# --- Validate environment ---
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION="${AWS_REGION:-us-east-1}"
ENVIRONMENT="${ENVIRONMENT:-dev}"

echo "=== KEDA Ownership Migration ==="
echo "Account:     ${ACCOUNT_ID}"
echo "Region:      ${REGION}"
echo "Environment: ${ENVIRONMENT}"
echo "Source:      ${SRC_DIR} (Phase 8 — agent-factory)"
echo "Destination: ${DST_DIR} (Phase 7 — webhook-ingress)"
echo ""

# --- Init both modules ---
echo "--- Initializing source module (agent-factory/infra) ---"
cd "${SRC_DIR}"
terraform init -input=false -reconfigure \
  -backend-config="bucket=adp-terraform-state-${ACCOUNT_ID}" \
  -backend-config="key=${ENVIRONMENT}/modules/agent-factory/terraform.tfstate" \
  -backend-config="region=${REGION}" \
  -backend-config="encrypt=true" \
  -backend-config="dynamodb_table=adp-terraform-locks" \
  > /dev/null

echo "--- Initializing destination module (webhook-ingress/infra) ---"
cd "${DST_DIR}"
terraform init -input=false -reconfigure \
  -backend-config="bucket=adp-terraform-state-${ACCOUNT_ID}" \
  -backend-config="key=${ENVIRONMENT}/modules/webhook-ingress/terraform.tfstate" \
  -backend-config="region=${REGION}" \
  -backend-config="encrypt=true" \
  -backend-config="dynamodb_table=adp-terraform-locks" \
  > /dev/null

# --- Check source state for KEDA resources ---
echo ""
echo "--- Checking source state for KEDA resources ---"
cd "${SRC_DIR}"

RESOURCES_TO_MOVE=(
  "aws_iam_role.keda_operator"
  "aws_iam_role_policy.keda_operator_sqs"
  "helm_release.keda"
)

FOUND_RESOURCES=()
for res in "${RESOURCES_TO_MOVE[@]}"; do
  if terraform state show "${res}" >/dev/null 2>&1; then
    FOUND_RESOURCES+=("${res}")
    echo "  FOUND: ${res}"
  else
    echo "  MISSING: ${res} (already migrated or never existed)"
  fi
done

if [[ ${#FOUND_RESOURCES[@]} -eq 0 ]]; then
  echo ""
  echo "No KEDA resources found in source state. Migration may have already run."
  echo "Verify with: cd ${DST_DIR} && terraform state list | grep keda"
  exit 0
fi

echo ""
echo "Found ${#FOUND_RESOURCES[@]} resource(s) to migrate."

if [[ "${DRY_RUN}" == "true" ]]; then
  echo ""
  echo "[DRY RUN] Would remove from source and import into destination:"
  for res in "${FOUND_RESOURCES[@]}"; do
    echo "  ${res}"
  done
  echo ""
  echo "Run without --dry-run to execute the migration."
  exit 0
fi

# --- Step 1: Pull resource details from source state ---
echo ""
echo "--- Step 1: Extracting resource IDs from source state ---"

declare -A RESOURCE_IDS

for res in "${FOUND_RESOURCES[@]}"; do
  # Get the resource ID from state show
  case "${res}" in
    "aws_iam_role.keda_operator")
      RESOURCE_IDS["${res}"]="adp-${ENVIRONMENT}-keda-operator-role"
      ;;
    "aws_iam_role_policy.keda_operator_sqs")
      RESOURCE_IDS["${res}"]="adp-${ENVIRONMENT}-keda-operator-role:sqs-scaler-read"
      ;;
    "helm_release.keda")
      RESOURCE_IDS["${res}"]="keda"
      ;;
  esac
  echo "  ${res} => ${RESOURCE_IDS[${res}]}"
done

# --- Step 2: Remove from source state ---
echo ""
echo "--- Step 2: Removing resources from source state (agent-factory) ---"
cd "${SRC_DIR}"

for res in "${FOUND_RESOURCES[@]}"; do
  echo "  Removing: ${res}"
  terraform state rm "${res}"
done

# --- Step 3: Import into destination state ---
echo ""
echo "--- Step 3: Importing resources into destination state (webhook-ingress) ---"
cd "${DST_DIR}"

for res in "${FOUND_RESOURCES[@]}"; do
  echo "  Importing: ${res} (ID: ${RESOURCE_IDS[${res}]})"
  terraform import "${res}" "${RESOURCE_IDS[${res}]}" || {
    echo "  WARNING: Import failed for ${res}. You may need to run terraform import manually."
    echo "  Command: cd ${DST_DIR} && terraform import '${res}' '${RESOURCE_IDS[${res}]}'"
  }
done

# --- Step 4: Verify ---
echo ""
echo "--- Step 4: Verification ---"
echo ""
echo "Source state (should have NO keda resources):"
cd "${SRC_DIR}"
terraform state list 2>/dev/null | grep -i keda || echo "  (none — correct)"

echo ""
echo "Destination state (should have keda resources):"
cd "${DST_DIR}"
terraform state list 2>/dev/null | grep -i keda || echo "  (none — ERROR: import may have failed)"

echo ""
echo "=== Migration complete ==="
echo ""
echo "Next steps:"
echo "  1. Run webhook-ingress-deploy workflow — should plan as no-op for KEDA."
echo "  2. Run agent-factory-infra-apply workflow — should plan as no-op (KEDA removed from state)."
echo "  3. Verify KEDA pods are still running: kubectl get pods -n keda"
