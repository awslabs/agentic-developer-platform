#!/usr/bin/env bash
# =============================================================================
# Validate Neptune IRSA inline policy + cleanup out-of-band managed attachment
# =============================================================================
# Verifies that the Terraform-managed inline neptune-db policy exists on the
# IRSA role, and optionally detaches the redundant out-of-band managed policy
# that was attached via CLI before neptune_enabled was flipped to true.
#
# Usage:
#   ./validate-neptune-irsa.sh              # validate only (dry run)
#   ./validate-neptune-irsa.sh --cleanup    # validate + detach managed policy
#
# Environment:
#   ENVIRONMENT   - defaults to "dev"
#   AWS_REGION    - defaults to "us-east-1"
# =============================================================================
set -euo pipefail

ENVIRONMENT="${ENVIRONMENT:-dev}"
AWS_REGION="${AWS_REGION:-us-east-1}"
CLEANUP=false

for arg in "$@"; do
  case "$arg" in
    --cleanup) CLEANUP=true ;;
    *) echo "Unknown argument: $arg"; exit 1 ;;
  esac
done

ROLE_NAME="adp-${ENVIRONMENT}-agent-context-irsa"
INLINE_POLICY_NAME="adp-${ENVIRONMENT}-agent-context-neptune"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
MANAGED_POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/adp-${ENVIRONMENT}-eks-cluster-neptune-access"

echo "=============================================="
echo "Neptune IRSA Policy Validation"
echo "=============================================="
echo "Role:            ${ROLE_NAME}"
echo "Inline policy:   ${INLINE_POLICY_NAME}"
echo "Managed policy:  ${MANAGED_POLICY_ARN}"
echo "Cleanup mode:    ${CLEANUP}"
echo "=============================================="

PASS=0
FAIL=0

check_pass() { echo "  [PASS] $1"; PASS=$((PASS + 1)); }
check_fail() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }

# --- Check 1: IRSA role exists ---
echo ""
echo "--- Check 1: IRSA Role ---"
if aws iam get-role --role-name "${ROLE_NAME}" --query 'Role.RoleName' --output text >/dev/null 2>&1; then
  check_pass "Role ${ROLE_NAME} exists"
else
  check_fail "Role ${ROLE_NAME} not found"
  echo "Cannot proceed without the IRSA role."
  exit 1
fi

# --- Check 2: TF-managed inline policy exists with neptune-db:* ---
echo ""
echo "--- Check 2: Terraform-Managed Inline Policy ---"
INLINE_POLICY_DOC=$(aws iam get-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-name "${INLINE_POLICY_NAME}" \
  --query 'PolicyDocument' \
  --output json 2>/dev/null || echo "NOT_FOUND")

if [ "${INLINE_POLICY_DOC}" = "NOT_FOUND" ]; then
  check_fail "Inline policy ${INLINE_POLICY_NAME} not found on role"
  echo ""
  echo "  The inline policy has not materialized. Ensure neptune_enabled=true"
  echo "  in environments/dev/modules/agent-context.tfvars and run infra-apply."
else
  # Verify it contains neptune-db:* action
  if echo "${INLINE_POLICY_DOC}" | grep -q "neptune-db:" ; then
    check_pass "Inline policy ${INLINE_POLICY_NAME} exists with neptune-db permissions"
  else
    check_fail "Inline policy exists but does not contain neptune-db actions"
  fi

  # Verify resource ARN pattern
  EXPECTED_RESOURCE="arn:aws:neptune-db:${AWS_REGION}:${ACCOUNT_ID}:*/*"
  if echo "${INLINE_POLICY_DOC}" | grep -q "${EXPECTED_RESOURCE}"; then
    check_pass "Resource ARN covers ${EXPECTED_RESOURCE}"
  else
    # Check for partition-aware pattern (aws-us-gov, aws-cn)
    if echo "${INLINE_POLICY_DOC}" | grep -q "neptune-db:${AWS_REGION}:${ACCOUNT_ID}"; then
      check_pass "Resource ARN covers the correct region and account"
    else
      check_fail "Resource ARN does not match expected pattern"
    fi
  fi
fi

# --- Check 3: Out-of-band managed policy attachment ---
echo ""
echo "--- Check 3: Out-of-Band Managed Policy ---"
ATTACHED_POLICIES=$(aws iam list-attached-role-policies \
  --role-name "${ROLE_NAME}" \
  --query "AttachedPolicies[?PolicyArn=='${MANAGED_POLICY_ARN}'].PolicyName" \
  --output text 2>/dev/null || echo "")

if [ -n "${ATTACHED_POLICIES}" ]; then
  if [ "${CLEANUP}" = "true" ]; then
    echo "  [INFO] Detaching redundant managed policy: ${MANAGED_POLICY_ARN}"
    if aws iam detach-role-policy \
      --role-name "${ROLE_NAME}" \
      --policy-arn "${MANAGED_POLICY_ARN}" 2>/dev/null; then
      check_pass "Detached out-of-band managed policy (drift eliminated)"
    else
      check_fail "Failed to detach managed policy"
    fi
  else
    echo "  [WARN] Out-of-band managed policy still attached: ${ATTACHED_POLICIES}"
    echo "         Run with --cleanup to detach it, or manually:"
    echo "         aws iam detach-role-policy --role-name ${ROLE_NAME} --policy-arn ${MANAGED_POLICY_ARN}"
    # Not a failure — the inline policy provides coverage; this is just drift
    check_pass "Managed policy present (redundant but not harmful; use --cleanup to remove)"
  fi
else
  check_pass "No out-of-band managed policy attached (clean state)"
fi

# --- Summary ---
echo ""
echo "=============================================="
echo "Summary: PASS=${PASS}  FAIL=${FAIL}"
echo "=============================================="

if [ "${FAIL}" -gt 0 ]; then
  echo "Some checks failed. See above for details."
  exit 1
else
  echo "All checks passed!"
  exit 0
fi
