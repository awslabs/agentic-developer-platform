#!/bin/bash
# =============================================================================
# test-force-delete-secrets-protect.sh — Unit tests for is_protected() function
# =============================================================================
# Tests the protect-list patterns in force-delete-secrets.sh to ensure
# critical secrets (GitHub App creds, Terraform state, RDS) are never deleted.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAILURES=0
PASSES=0

# Source only the is_protected function from the main script.
# We extract it to avoid executing the rest of the script.
eval "$(sed -n '/^is_protected()/,/^}/p' "$SCRIPT_DIR/force-delete-secrets.sh")"

assert_protected() {
  local name="$1"
  if is_protected "$name"; then
    echo "  PASS: '$name' → PROTECTED"
    PASSES=$((PASSES + 1))
  else
    echo "  FAIL: '$name' → expected PROTECTED, got NOT protected"
    FAILURES=$((FAILURES + 1))
  fi
}

assert_not_protected() {
  local name="$1"
  if is_protected "$name"; then
    echo "  FAIL: '$name' → expected NOT protected, got PROTECTED"
    FAILURES=$((FAILURES + 1))
  else
    echo "  PASS: '$name' → NOT protected (deletable)"
    PASSES=$((PASSES + 1))
  fi
}

echo "=== Testing is_protected() patterns ==="
echo ""

echo "--- Legacy GitHub App patterns (adp/gh-app-*, adp/*/gh-app-*) ---"
assert_protected "adp/gh-app-my-app-id"
assert_protected "adp/gh-app-my-app-key"
assert_protected "adp/dev/gh-app-platform-id"
assert_protected "adp/prod/gh-app-platform-key"
echo ""

echo "--- Webhook-ingress GitHub App pattern (adp/*/github-app/*) ---"
assert_protected "adp/dev/github-app/adp-agent-platform-id"
assert_protected "adp/dev/github-app/adp-agent-platform-key"
assert_protected "adp/prod/github-app/custom-app-id"
assert_protected "adp/staging/github-app/anything"
echo ""

echo "--- Terraform state backend (adp-terraform-*) ---"
assert_protected "adp-terraform-state-lock"
assert_protected "adp-terraform-backend"
echo ""

echo "--- AWS-managed RDS secrets (rds!*) ---"
assert_protected "rds!cluster-abc123"
assert_protected "rds!db-instance-xyz"
echo ""

echo "--- Secrets that SHOULD be deletable ---"
assert_not_protected "bedrockgw-dev-some-secret"
assert_not_protected "bedrockgw-dev-api-key"
assert_not_protected "adp/dev/gateway/test-key"
assert_not_protected "adp/dev/gateway/oauth-stub"
assert_not_protected "my-random-secret"
assert_not_protected "adp/dev/some-other-thing"
echo ""

echo "=== Results ==="
echo "Passed: $PASSES"
echo "Failed: $FAILURES"
echo ""

if [ "$FAILURES" -gt 0 ]; then
  echo "FAILED: $FAILURES test(s) did not pass."
  exit 1
fi

echo "All tests passed."
exit 0
