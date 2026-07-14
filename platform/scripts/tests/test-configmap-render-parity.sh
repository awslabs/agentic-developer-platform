#!/bin/bash
# =============================================================================
# test-configmap-render-parity.sh — Ensures deploy-all.sh and gateway-deploy.yml
# substitute the SAME set of configmap placeholders
# =============================================================================
# Extracts all __PLACEHOLDER__ tokens from modules/gateway/k8s/configmap.yaml
# and verifies that BOTH render implementations (deploy-all.sh sed block and
# gateway-deploy.yml sed block) substitute every one. Prevents silent drift when
# new configmap keys are added to only one renderer.
#
# Issue: #3856
# Run:   bash platform/scripts/tests/test-configmap-render-parity.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"

CONFIGMAP="$ROOT_DIR/modules/gateway/k8s/configmap.yaml"
DEPLOY_ALL="$ROOT_DIR/platform/scripts/deploy-all.sh"
WORKFLOW="$ROOT_DIR/.github/workflows/gateway-deploy.yml"

# ---------------------------------------------------------------------------
# Intentional differences allowlist.
# Each entry must have an inline justification explaining why the placeholder
# is handled differently between the two renderers. If you're adding an entry
# here, think twice — the whole point of this test is to catch unintentional
# divergence.
# ---------------------------------------------------------------------------
declare -A ALLOWLIST=(
  # (none currently — all placeholders must be rendered by both implementations)
)

FAILURES=0
PASSES=0

pass() {
  echo "  PASS: $1"
  PASSES=$((PASSES + 1))
}

fail_test() {
  echo "  FAIL: $1"
  FAILURES=$((FAILURES + 1))
}

# ---------------------------------------------------------------------------
# Step 1: Extract all __PLACEHOLDER__ tokens from configmap.yaml
# ---------------------------------------------------------------------------
echo "=== Test: ConfigMap render parity (Issue #3856) ==="
echo ""

if [ ! -f "$CONFIGMAP" ]; then
  echo "ERROR: configmap.yaml not found at $CONFIGMAP"
  exit 1
fi

# Extract unique placeholder names (e.g., __AWS_REGION__, __DB_HOST__)
# Only match placeholders that appear as VALUES (after a colon + quotes), not in comments.
# Pattern: lines containing ":" followed by quoted __PLACEHOLDER__ value
PLACEHOLDERS=$(grep -v '^\s*#' "$CONFIGMAP" | grep -oE '__[A-Z_]+__' | sort -u)
PLACEHOLDER_COUNT=$(echo "$PLACEHOLDERS" | wc -l | tr -d ' ')

echo "Found $PLACEHOLDER_COUNT unique placeholders in configmap.yaml:"
echo "$PLACEHOLDERS" | sed 's/^/    /'
echo ""

# ---------------------------------------------------------------------------
# Step 2: Verify deploy-all.sh substitutes each placeholder
# ---------------------------------------------------------------------------
echo "--- Checking deploy-all.sh ---"
DEPLOY_ALL_MISSING=()
for placeholder in $PLACEHOLDERS; do
  # Check if allowlisted
  if [ -n "${ALLOWLIST[$placeholder]:-}" ]; then
    pass "$placeholder — allowlisted (${ALLOWLIST[$placeholder]})"
    continue
  fi
  # Look for the sed substitution pattern: s|__PLACEHOLDER__|...|g
  if grep -q "s|${placeholder}|" "$DEPLOY_ALL" 2>/dev/null; then
    pass "$placeholder — found in deploy-all.sh sed block"
  else
    fail_test "$placeholder — NOT found in deploy-all.sh sed block"
    DEPLOY_ALL_MISSING+=("$placeholder")
  fi
done
echo ""

# ---------------------------------------------------------------------------
# Step 3: Verify gateway-deploy.yml substitutes each placeholder
# ---------------------------------------------------------------------------
echo "--- Checking gateway-deploy.yml ---"
WORKFLOW_MISSING=()
for placeholder in $PLACEHOLDERS; do
  # Check if allowlisted
  if [ -n "${ALLOWLIST[$placeholder]:-}" ]; then
    pass "$placeholder — allowlisted (${ALLOWLIST[$placeholder]})"
    continue
  fi
  # Look for the sed substitution pattern: s|__PLACEHOLDER__|...|g
  if grep -q "s|${placeholder}|" "$WORKFLOW" 2>/dev/null; then
    pass "$placeholder — found in gateway-deploy.yml sed block"
  else
    fail_test "$placeholder — NOT found in gateway-deploy.yml sed block"
    WORKFLOW_MISSING+=("$placeholder")
  fi
done
echo ""

# ---------------------------------------------------------------------------
# Step 4: Check for hardcoded empty/false values that diverge from dynamic resolution
# ---------------------------------------------------------------------------
echo "--- Checking for hardcoded empty/literal sed substitutions in deploy-all.sh ---"
# A hardcoded empty substitution looks like: s|__PLACEHOLDER__||g (nothing between last two pipes)
# A hardcoded literal looks like: s|__PLACEHOLDER__|false|g or s|__PLACEHOLDER__|off|g
# These are acceptable ONLY if the workflow also uses the same literal value.
HARDCODED_DIVERGENCE=0
for placeholder in $PLACEHOLDERS; do
  if [ -n "${ALLOWLIST[$placeholder]:-}" ]; then continue; fi

  # Extract what deploy-all.sh substitutes for this placeholder
  DEPLOY_ALL_VALUE=$(grep "s|${placeholder}|" "$DEPLOY_ALL" 2>/dev/null | \
    sed -n "s/.*s|${placeholder}|\([^|]*\)|g.*/\1/p" | head -1)
  # Extract what the workflow substitutes
  WORKFLOW_VALUE=$(grep "s|${placeholder}|" "$WORKFLOW" 2>/dev/null | \
    sed -n "s/.*s|${placeholder}|\([^|]*\)|g.*/\1/p" | head -1)

  # Both are bare literals (no ${...} variable reference) AND they differ
  if [[ "$DEPLOY_ALL_VALUE" != *'${'* ]] && [[ "$WORKFLOW_VALUE" == *'${'* ]] && [ -z "$DEPLOY_ALL_VALUE" ]; then
    fail_test "$placeholder — deploy-all.sh hardcodes EMPTY while workflow resolves dynamically"
    HARDCODED_DIVERGENCE=$((HARDCODED_DIVERGENCE + 1))
  fi
done

if [ "$HARDCODED_DIVERGENCE" -eq 0 ]; then
  echo "  No hardcoded-empty divergences detected."
fi
echo ""

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo "=== Results: $PASSES passed, $FAILURES failed ==="

if [ ${#DEPLOY_ALL_MISSING[@]} -gt 0 ]; then
  echo ""
  echo "Missing from deploy-all.sh: ${DEPLOY_ALL_MISSING[*]}"
  echo "  → Add SSM resolution + sed substitution for these placeholders"
fi

if [ ${#WORKFLOW_MISSING[@]} -gt 0 ]; then
  echo ""
  echo "Missing from gateway-deploy.yml: ${WORKFLOW_MISSING[*]}"
  echo "  → Add SSM resolution + sed substitution for these placeholders"
fi

if [ "$FAILURES" -gt 0 ]; then
  echo ""
  echo "FAILED — configmap render implementations are out of sync"
  exit 1
fi
echo "ALL PASSED — both renderers cover all configmap placeholders"
exit 0
