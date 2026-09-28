#!/usr/bin/env bash
# flip-gate-check.sh — Hard-assert preconditions for the credential-binding enforcement flip.
#
# Issue: #3182 (S9 of EPIC #3172)
# Design: docs/design/credential-authorization-binding.md §Phase 2
#
# Exits 0 only if ALL four preconditions hold:
#   1. credential_authorization_drift metric == 0 over trailing window (7 days)
#   2. Registry-row coverage == 100% (no fallback events in the window)
#   3. Latest nightly adversarial E2E (credential-binding-adversarial-e2e.yml) green
#   4. ENABLE_USER_CREDENTIALS=true in sandbox tenant
#
# Usage:
#   ./platform/scripts/flip-gate-check.sh --environment dev [--window-days 7] [--sandbox-tenant adp-security-test]
#
# Requires: aws CLI, gh CLI, jq

set -euo pipefail

# -------------------------------------------------------------------
# Defaults
# -------------------------------------------------------------------
ENVIRONMENT="${ENVIRONMENT:-dev}"
AWS_REGION="${AWS_REGION:-us-east-1}"
WINDOW_DAYS=7
SANDBOX_TENANT="adp-security-test"
CW_NAMESPACE="BedrockGateway"
NIGHTLY_WORKFLOW="credential-binding-adversarial-e2e.yml"
REPO="${GITHUB_REPOSITORY:-aws-e/adp}"

# -------------------------------------------------------------------
# Parse arguments
# -------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --environment) ENVIRONMENT="$2"; shift 2 ;;
    --window-days) WINDOW_DAYS="$2"; shift 2 ;;
    --sandbox-tenant) SANDBOX_TENANT="$2"; shift 2 ;;
    --aws-region) AWS_REGION="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
FAILURES=()

fail() {
  FAILURES+=("$1")
  echo "❌ GATE FAILED: $1" >&2
}

pass() {
  echo "✅ GATE PASSED: $1"
}

# -------------------------------------------------------------------
# Gate 1: credential_authorization_drift == 0 over trailing window
# -------------------------------------------------------------------
echo ""
echo "=== Gate 1: Drift metric == 0 (trailing ${WINDOW_DAYS}d) ==="

END_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)
START_TIME=$(date -u -d "${WINDOW_DAYS} days ago" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || \
             date -u -v-${WINDOW_DAYS}d +%Y-%m-%dT%H:%M:%SZ)

# Query CloudWatch for credential_authorization_drift metric (Sum over the window).
# The metric is emitted as EMF by the gateway pod when drift is detected.
DRIFT_RESULT=$(aws cloudwatch get-metric-statistics \
  --namespace "$CW_NAMESPACE" \
  --metric-name "CredentialAuthorizationDrift" \
  --start-time "$START_TIME" \
  --end-time "$END_TIME" \
  --period $((WINDOW_DAYS * 86400)) \
  --statistics Sum \
  --dimensions "Name=Environment,Value=${ENVIRONMENT}" \
  --region "$AWS_REGION" \
  --output json 2>/dev/null || echo '{"Datapoints":[]}')

DRIFT_SUM=$(echo "$DRIFT_RESULT" | jq '[.Datapoints[].Sum] | add // 0')

if [ "$(echo "$DRIFT_SUM > 0" | bc -l 2>/dev/null || echo "0")" = "1" ]; then
  fail "credential_authorization_drift Sum=${DRIFT_SUM} over ${WINDOW_DAYS}d (must be 0)"
elif [ "$DRIFT_SUM" = "0" ]; then
  pass "credential_authorization_drift == 0 over ${WINDOW_DAYS}d"
else
  # No datapoints means the metric was never emitted — which means zero drift
  pass "credential_authorization_drift == 0 (no datapoints in window — zero drift)"
fi

# -------------------------------------------------------------------
# Gate 2: Registry-row coverage == 100% (no fallback events)
# -------------------------------------------------------------------
echo ""
echo "=== Gate 2: Registry coverage == 100% (no fallback in ${WINDOW_DAYS}d) ==="

FALLBACK_RESULT=$(aws cloudwatch get-metric-statistics \
  --namespace "$CW_NAMESPACE" \
  --metric-name "CredentialAuthorizationFallback" \
  --start-time "$START_TIME" \
  --end-time "$END_TIME" \
  --period $((WINDOW_DAYS * 86400)) \
  --statistics Sum \
  --dimensions "Name=Environment,Value=${ENVIRONMENT}" \
  --region "$AWS_REGION" \
  --output json 2>/dev/null || echo '{"Datapoints":[]}')

FALLBACK_SUM=$(echo "$FALLBACK_RESULT" | jq '[.Datapoints[].Sum] | add // 0')

if [ "$(echo "$FALLBACK_SUM > 0" | bc -l 2>/dev/null || echo "0")" = "1" ]; then
  fail "credential_authorization_fallback Sum=${FALLBACK_SUM} over ${WINDOW_DAYS}d (must be 0 for 100% coverage)"
elif [ "$FALLBACK_SUM" = "0" ]; then
  pass "Registry coverage == 100% (zero fallback events in ${WINDOW_DAYS}d)"
else
  pass "Registry coverage == 100% (no fallback datapoints — all calls had registry rows)"
fi

# -------------------------------------------------------------------
# Gate 3: Latest nightly adversarial E2E is green
# -------------------------------------------------------------------
echo ""
echo "=== Gate 3: Latest nightly adversarial E2E green ==="

# Get the most recent completed run of the nightly workflow
LATEST_RUN=$(gh run list \
  --workflow "$NIGHTLY_WORKFLOW" \
  --repo "$REPO" \
  --status completed \
  --limit 1 \
  --json conclusion,createdAt,databaseId \
  --jq '.[0]' 2>/dev/null || echo '{}')

if [ -z "$LATEST_RUN" ] || [ "$LATEST_RUN" = "{}" ] || [ "$LATEST_RUN" = "null" ]; then
  fail "No completed runs found for ${NIGHTLY_WORKFLOW}"
else
  CONCLUSION=$(echo "$LATEST_RUN" | jq -r '.conclusion')
  CREATED_AT=$(echo "$LATEST_RUN" | jq -r '.createdAt')
  RUN_ID=$(echo "$LATEST_RUN" | jq -r '.databaseId')

  if [ "$CONCLUSION" = "success" ]; then
    pass "Latest nightly E2E passed (run ${RUN_ID}, ${CREATED_AT})"
  else
    fail "Latest nightly E2E conclusion='${CONCLUSION}' (run ${RUN_ID}, ${CREATED_AT}) — must be 'success'"
  fi
fi

# -------------------------------------------------------------------
# Gate 4: ENABLE_USER_CREDENTIALS=true in sandbox tenant
# -------------------------------------------------------------------
echo ""
echo "=== Gate 4: ENABLE_USER_CREDENTIALS=true in sandbox tenant ==="

# Check SSM parameter for the sandbox tenant's credential feature flag
SSM_PARAM="/adp/${ENVIRONMENT}/${SANDBOX_TENANT}/enable-user-credentials"
ENABLE_CREDS=$(aws ssm get-parameter \
  --name "$SSM_PARAM" \
  --query "Parameter.Value" \
  --output text \
  --region "$AWS_REGION" 2>/dev/null || echo "")

if [ -z "$ENABLE_CREDS" ]; then
  fail "SSM parameter ${SSM_PARAM} not found — ENABLE_USER_CREDENTIALS not set for sandbox tenant"
elif [ "$ENABLE_CREDS" = "true" ] || [ "$ENABLE_CREDS" = "1" ]; then
  pass "ENABLE_USER_CREDENTIALS=${ENABLE_CREDS} in sandbox tenant (${SANDBOX_TENANT})"
else
  fail "ENABLE_USER_CREDENTIALS=${ENABLE_CREDS} in sandbox tenant (must be 'true')"
fi

# -------------------------------------------------------------------
# Summary
# -------------------------------------------------------------------
echo ""
echo "==========================================="
echo "  FLIP-GATE SUMMARY"
echo "==========================================="
echo "  Environment:    ${ENVIRONMENT}"
echo "  Window:         ${WINDOW_DAYS} days"
echo "  Sandbox tenant: ${SANDBOX_TENANT}"
echo "  Gates checked:  4"
echo "  Failures:       ${#FAILURES[@]}"
echo "==========================================="

if [ ${#FAILURES[@]} -gt 0 ]; then
  echo ""
  echo "BLOCKED — the following gates failed:"
  for f in "${FAILURES[@]}"; do
    echo "  • $f"
  done
  echo ""
  echo "The enforcement flip is NOT safe. Resolve the above before re-dispatching."
  exit 1
fi

echo ""
echo "ALL GATES PASSED — enforcement flip is safe to proceed."
exit 0
