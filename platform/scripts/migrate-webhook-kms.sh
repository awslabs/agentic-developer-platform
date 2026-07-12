#!/usr/bin/env bash
# =============================================================================
# One-shot migration: move webhook-secrets CMK + alias from webhook-ingress
# terraform state to platform infra terraform state.
# =============================================================================
# Issue #3789: The webhook-secrets CMK (alias/adp-<env>-webhook-secrets) was
# previously owned by modules/agent-factory/webhook-ingress/infra. It is now
# owned by platform/infra (shared infrastructure consumed by both gateway and
# webhook-ingress). This script performs the cross-state migration:
#
#   Phase A: Remove from webhook-ingress state (state rm — does NOT destroy)
#   Phase B: Import into platform state
#   Phase C: Verify all three modules plan with 0 destroys on KMS resources
#
# The CMK itself never moves in AWS — this is a state-only operation. No
# re-encryption, no key rotation event, no data-path change.
#
# Usage:
#   ./platform/scripts/migrate-webhook-kms.sh [--dry-run]
#
# Prerequisites:
#   - AWS credentials configured (same account where the CMK exists)
#   - terraform CLI available
#   - The webhook-ingress module has been deployed (CMK exists in AWS)
#   - The PR that moves CMK definition to platform/infra has been merged
#
# Affected accounts (execute in this order — platform account is canary):
#   - Platform: 879318057152
#   - Deployed instances: run during next --update or manual dispatch
#
# HARD CONSTRAINT: This script aborts if ANY terraform plan shows a destroy
# of the CMK or its alias. A destroyed CMK makes all existing ciphertexts
# (adp/<env>/github-app/*, webhook secrets) permanently undecryptable.
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SRC_DIR="${REPO_ROOT}/modules/agent-factory/webhook-ingress/infra"
DST_DIR="${REPO_ROOT}/platform/infra"

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
  echo "[DRY RUN] Will show what would be migrated without making changes."
fi

# --- Validate environment ---
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION="${AWS_REGION:-us-east-1}"
ENVIRONMENT="${ENVIRONMENT:-dev}"

echo "=== Webhook-Secrets CMK Ownership Migration (Issue #3789) ==="
echo "Account:     ${ACCOUNT_ID}"
echo "Region:      ${REGION}"
echo "Environment: ${ENVIRONMENT}"
echo "Source:      ${SRC_DIR} (webhook-ingress)"
echo "Destination: ${DST_DIR} (platform infra)"
echo ""

# --- Verify the CMK exists in AWS ---
ALIAS_NAME="alias/adp-${ENVIRONMENT}-webhook-secrets"
echo "--- Verifying CMK exists in AWS ---"
KEY_ID=$(aws kms describe-key --key-id "${ALIAS_NAME}" --region "${REGION}" \
  --query "KeyMetadata.KeyId" --output text 2>/dev/null) || {
  echo "ERROR: KMS alias '${ALIAS_NAME}' not found in account ${ACCOUNT_ID}."
  echo "This script is for migrating EXISTING keys. On a fresh account, the"
  echo "platform infra apply will create the key — no migration needed."
  exit 1
}

KEY_ARN=$(aws kms describe-key --key-id "${ALIAS_NAME}" --region "${REGION}" \
  --query "KeyMetadata.Arn" --output text)
ALIAS_ARN="arn:aws:kms:${REGION}:${ACCOUNT_ID}:${ALIAS_NAME}"

echo "  Key ID:    ${KEY_ID}"
echo "  Key ARN:   ${KEY_ARN}"
echo "  Alias ARN: ${ALIAS_ARN}"
echo ""

# --- Init both modules ---
echo "--- Initializing source module (webhook-ingress/infra) ---"
cd "${SRC_DIR}"
terraform init -input=false -reconfigure \
  -backend-config="bucket=adp-terraform-state-${ACCOUNT_ID}" \
  -backend-config="key=${ENVIRONMENT}/modules/webhook-ingress/terraform.tfstate" \
  -backend-config="region=${REGION}" \
  -backend-config="encrypt=true" \
  -backend-config="dynamodb_table=adp-terraform-locks" \
  > /dev/null

echo "--- Initializing destination module (platform/infra) ---"
cd "${DST_DIR}"
terraform init -input=false -reconfigure \
  -backend-config="bucket=adp-terraform-state-${ACCOUNT_ID}" \
  -backend-config="key=${ENVIRONMENT}/platform/terraform.tfstate" \
  -backend-config="region=${REGION}" \
  -backend-config="encrypt=true" \
  -backend-config="dynamodb_table=adp-terraform-locks" \
  > /dev/null

# --- Check source state for KMS resources ---
echo ""
echo "--- Checking source state for webhook-secrets KMS resources ---"
cd "${SRC_DIR}"

RESOURCES_TO_MOVE=(
  "aws_kms_key.secrets"
  "aws_kms_alias.secrets"
)

FOUND_RESOURCES=()
for res in "${RESOURCES_TO_MOVE[@]}"; do
  if terraform state show "${res}" >/dev/null 2>&1; then
    FOUND_RESOURCES+=("${res}")
    echo "  FOUND: ${res}"
  else
    echo "  MISSING: ${res} (already migrated or never existed in this state)"
  fi
done

if [[ ${#FOUND_RESOURCES[@]} -eq 0 ]]; then
  echo ""
  echo "No webhook-secrets KMS resources found in source state."
  echo "Migration may have already run. Verify with:"
  echo "  cd ${DST_DIR} && terraform state list | grep webhook_secrets"
  exit 0
fi

echo ""
echo "Found ${#FOUND_RESOURCES[@]} resource(s) to migrate."

# --- Check destination state doesn't already have them ---
echo ""
echo "--- Checking destination state for conflicts ---"
cd "${DST_DIR}"

DST_RESOURCES=(
  "aws_kms_key.webhook_secrets"
  "aws_kms_alias.webhook_secrets"
)

for res in "${DST_RESOURCES[@]}"; do
  if terraform state show "${res}" >/dev/null 2>&1; then
    echo "  CONFLICT: ${res} already exists in destination state!"
    echo "  The migration may have partially completed. Check manually."
    exit 1
  fi
done
echo "  No conflicts — destination state is clean."

if [[ "${DRY_RUN}" == "true" ]]; then
  echo ""
  echo "[DRY RUN] Would execute:"
  echo "  Phase A: terraform state rm (webhook-ingress) for:"
  for res in "${FOUND_RESOURCES[@]}"; do
    echo "    ${res}"
  done
  echo "  Phase B: terraform import (platform) for:"
  echo "    aws_kms_key.webhook_secrets  => ${KEY_ID}"
  echo "    aws_kms_alias.webhook_secrets => ${ALIAS_ARN}"
  echo "  Phase C: terraform plan on all three modules (verify 0 KMS destroys)"
  echo ""
  echo "Run without --dry-run to execute the migration."
  exit 0
fi

# =============================================================================
# Phase A: Remove from webhook-ingress state
# =============================================================================
echo ""
echo "=== Phase A: Removing KMS resources from webhook-ingress state ==="
echo "  (This does NOT destroy the key in AWS — only removes state tracking)"
cd "${SRC_DIR}"

for res in "${FOUND_RESOURCES[@]}"; do
  echo "  Removing: ${res}"
  terraform state rm "${res}"
done

echo "  Phase A complete."

# =============================================================================
# Phase B: Import into platform state
# =============================================================================
echo ""
echo "=== Phase B: Importing KMS resources into platform state ==="
cd "${DST_DIR}"

echo "  Importing: aws_kms_key.webhook_secrets (ID: ${KEY_ID})"
terraform import \
  -var-file="../../environments/${ENVIRONMENT}/platform.tfvars" \
  "aws_kms_key.webhook_secrets" "${KEY_ID}" || {
  echo ""
  echo "ERROR: Import of KMS key failed!"
  echo "The key has been removed from webhook-ingress state (Phase A) but NOT"
  echo "imported into platform state. To recover:"
  echo "  cd ${DST_DIR}"
  echo "  terraform import -var-file=../../environments/${ENVIRONMENT}/platform.tfvars \\"
  echo "    aws_kms_key.webhook_secrets ${KEY_ID}"
  exit 1
}

echo "  Importing: aws_kms_alias.webhook_secrets (ID: ${ALIAS_ARN})"
terraform import \
  -var-file="../../environments/${ENVIRONMENT}/platform.tfvars" \
  "aws_kms_alias.webhook_secrets" "${ALIAS_ARN}" || {
  echo ""
  echo "ERROR: Import of KMS alias failed!"
  echo "The key was imported but the alias was not. To recover:"
  echo "  cd ${DST_DIR}"
  echo "  terraform import -var-file=../../environments/${ENVIRONMENT}/platform.tfvars \\"
  echo "    aws_kms_alias.webhook_secrets ${ALIAS_ARN}"
  exit 1
}

echo "  Phase B complete."

# =============================================================================
# Phase C: Verify 0-destroy plans
# =============================================================================
echo ""
echo "=== Phase C: Verifying terraform plans (0 KMS destroys required) ==="

check_no_kms_destroy() {
  local module_name="$1"
  local plan_output="$2"

  # Check for any destroy of KMS key or alias resources
  if echo "${plan_output}" | grep -qE '# aws_kms_(key|alias)\..* will be destroyed'; then
    echo ""
    echo "╔═══════════════════════════════════════════════════════════════════╗"
    echo "║  HARD STOP: ${module_name} plan shows KMS DESTROY!              ║"
    echo "║  Aborting. DO NOT APPLY until this is resolved.                 ║"
    echo "║  The CMK destruction would make all existing ciphertexts        ║"
    echo "║  permanently undecryptable.                                     ║"
    echo "╚═══════════════════════════════════════════════════════════════════╝"
    echo ""
    echo "Plan output (KMS lines):"
    echo "${plan_output}" | grep -E 'aws_kms_(key|alias)' || true
    exit 1
  fi
}

# A failed plan (init error, bad creds, missing vars) must NOT read as
# "no destroys found" — that is false confidence at the exact point the
# hard constraint lives. Plain `terraform plan` exits 0 on success (with or
# without changes) and 1 on error.
check_plan_succeeded() {
  local module_name="$1"
  local rc="$2"
  local plan_output="$3"
  if [ "${rc}" -ne 0 ]; then
    echo ""
    echo "ERROR: ${module_name} terraform plan FAILED (exit ${rc}) — cannot verify the 0-destroy constraint."
    echo "Last 20 lines of plan output:"
    echo "${plan_output}" | tail -20
    exit 1
  fi
}

# Plan: platform infra
echo "  Planning: platform/infra..."
cd "${DST_DIR}"
PLAN_RC=0
PLATFORM_PLAN=$(terraform plan -var-file="../../environments/${ENVIRONMENT}/platform.tfvars" \
  -no-color 2>&1) || PLAN_RC=$?
check_plan_succeeded "platform" "${PLAN_RC}" "${PLATFORM_PLAN}"
check_no_kms_destroy "platform" "${PLATFORM_PLAN}"
echo "    ✓ platform plan OK (no KMS destroys)"

# Plan: webhook-ingress
echo "  Planning: webhook-ingress/infra..."
cd "${SRC_DIR}"
PLAN_RC=0
WI_PLAN=$(terraform plan -var-file="../../../../environments/${ENVIRONMENT}/modules/webhook-ingress.tfvars" \
  -no-color 2>&1) || PLAN_RC=$?
check_plan_succeeded "webhook-ingress" "${PLAN_RC}" "${WI_PLAN}"
check_no_kms_destroy "webhook-ingress" "${WI_PLAN}"
echo "    ✓ webhook-ingress plan OK (no KMS destroys)"

# Plan: gateway (informational — the grant change is expected)
echo "  Planning: gateway/infra..."
cd "${REPO_ROOT}/modules/gateway/infra"
PLAN_RC=0
GW_PLAN=$(terraform plan -var-file="../../../environments/${ENVIRONMENT}/modules/gateway.tfvars" \
  -no-color 2>&1) || PLAN_RC=$?
check_plan_succeeded "gateway" "${PLAN_RC}" "${GW_PLAN}"
check_no_kms_destroy "gateway" "${GW_PLAN}"
echo "    ✓ gateway plan OK (no KMS destroys)"

echo ""
echo "=== Migration Complete ==="
echo ""
echo "Summary:"
echo "  - CMK ${KEY_ID} (${ALIAS_NAME}) is now managed by platform/infra"
echo "  - Webhook-ingress references the key by alias (data source)"
echo "  - Gateway grant is unconditional (no flag needed)"
echo ""
echo "Next steps:"
echo "  1. Apply platform/infra (may show in-place updates for tags/policy)"
echo "  2. Apply webhook-ingress (should show key/alias removed, 0 destroy)"
echo "  3. Apply gateway (grant becomes unconditional — 1 create or in-place)"
echo ""
echo "If the 925 account has the hand-patched policy:"
echo "  The gateway apply will adopt it cleanly (exact TF name/shape match)."
