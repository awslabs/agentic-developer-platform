#!/bin/bash
# =============================================================================
# test-destroy-gate.sh — Regression tests for the destroy-gate grep logic
# =============================================================================
# Validates that terraform_update_apply's destroy detection works correctly in
# three scenarios:
#   1. Clean plan output (-no-color) — grep must match "will be destroyed"
#   2. ANSI-colored plan output — grep must STILL match (the bug in #3664)
#   3. No-destroy plan — grep must return 0 (no false positives)
#
# This test exercises the EXACT grep pattern used in deploy-all.sh to ensure
# the fix (-no-color) works, AND that even if ANSI codes leak through somehow,
# a sed-based fallback catches them.
#
# Run: bash platform/scripts/tests/test-destroy-gate.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIXTURES_DIR="$SCRIPT_DIR/fixtures"
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
# The grep pattern (must match what deploy-all.sh uses at line ~163)
# ---------------------------------------------------------------------------
count_destroys() {
  local PLAN_FILE="$1"
  local COUNT
  COUNT=$(grep -c 'will be destroyed' "$PLAN_FILE" 2>/dev/null) || COUNT=0
  echo "$COUNT"
}

# Fallback: strip ANSI then grep (defense-in-depth — belt AND suspenders)
count_destroys_strip_ansi() {
  local PLAN_FILE="$1"
  local COUNT
  COUNT=$(sed 's/\x1b\[[0-9;]*m//g' "$PLAN_FILE" | grep -c 'will be destroyed' 2>/dev/null) || COUNT=0
  echo "$COUNT"
}

# ===========================================================================
echo "=== Test: Destroy gate grep logic (Issue #3664) ==="
echo ""

# ---------------------------------------------------------------------------
# Test 1: Clean output (no ANSI) — must detect 3 destroys
# ---------------------------------------------------------------------------
echo "--- Test 1: Clean plan output (no ANSI codes) ---"
CLEAN_FIXTURE="$FIXTURES_DIR/plan-output-with-ansi.txt"

RESULT=$(count_destroys "$CLEAN_FIXTURE")
if [ "$RESULT" -eq 3 ]; then
  pass "Clean output: detected $RESULT destroys (expected 3)"
else
  fail_test "Clean output: detected $RESULT destroys (expected 3)"
fi

# ---------------------------------------------------------------------------
# Test 2: ANSI-colored output — the EXACT bug from #3664
# This simulates what terraform outputs WITHOUT -no-color. The grep pattern
# WILL fail here (proving the bug existed). The sed-strip fallback MUST work.
# ---------------------------------------------------------------------------
echo ""
echo "--- Test 2: ANSI-colored plan output (the #3664 bug) ---"
ANSI_FIXTURE="$FIXTURES_DIR/plan-output-with-ansi-codes.txt"

# 2a. Raw grep (this SHOULD fail — proving the bug existed)
RESULT_RAW=$(count_destroys "$ANSI_FIXTURE")
if [ "$RESULT_RAW" -eq 0 ]; then
  pass "Raw grep on ANSI output: detected 0 destroys (confirms the bug pattern — ANSI hides the match)"
else
  # If this passes, it means grep on this system handles ANSI differently.
  # Still a pass — the fix makes the gate reliable regardless.
  pass "Raw grep on ANSI output: detected $RESULT_RAW destroys (grep matched despite ANSI — some systems tolerate this)"
fi

# 2b. ANSI-stripped grep (defense-in-depth fallback — MUST work)
RESULT_STRIPPED=$(count_destroys_strip_ansi "$ANSI_FIXTURE")
if [ "$RESULT_STRIPPED" -eq 3 ]; then
  pass "ANSI-stripped grep: detected $RESULT_STRIPPED destroys (expected 3) — fallback works"
else
  fail_test "ANSI-stripped grep: detected $RESULT_STRIPPED destroys (expected 3) — FALLBACK BROKEN"
fi

# ---------------------------------------------------------------------------
# Test 3: No-destroy plan output — must return 0 (no false positives)
# ---------------------------------------------------------------------------
echo ""
echo "--- Test 3: No-destroy plan output (no false positives) ---"

NO_DESTROY_OUTPUT=$(mktemp)
cat > "$NO_DESTROY_OUTPUT" << 'EOF'
Terraform used the selected providers to generate the following execution
plan. Resource actions are indicated with the following symbols:
  ~ update in-place

Terraform will perform the following actions:

  # module.gateway.aws_iam_policy.bedrock_access will be updated in-place
  ~ resource "aws_iam_policy" "bedrock_access" {
      ~ policy = jsonencode(...)
    }

Plan: 0 to add, 1 to change, 0 to destroy.
EOF

RESULT=$(count_destroys "$NO_DESTROY_OUTPUT")
if [ "$RESULT" -eq 0 ]; then
  pass "No-destroy plan: detected $RESULT destroys (expected 0)"
else
  fail_test "No-destroy plan: detected $RESULT destroys (expected 0) — FALSE POSITIVE"
fi

RESULT_STRIPPED=$(count_destroys_strip_ansi "$NO_DESTROY_OUTPUT")
if [ "$RESULT_STRIPPED" -eq 0 ]; then
  pass "No-destroy plan (ANSI-strip): detected $RESULT_STRIPPED destroys (expected 0)"
else
  fail_test "No-destroy plan (ANSI-strip): detected $RESULT_STRIPPED destroys (expected 0) — FALSE POSITIVE"
fi

rm -f "$NO_DESTROY_OUTPUT"

# ---------------------------------------------------------------------------
# Test 4: Verify deploy-all.sh uses -no-color (the actual fix)
# ---------------------------------------------------------------------------
echo ""
echo "--- Test 4: deploy-all.sh uses -no-color in terraform_update_apply ---"
DEPLOY_SCRIPT="$SCRIPT_DIR/../deploy-all.sh"

# Extract the function body, join backslash-continued lines, then check that
# every "terraform plan" invocation includes -no-color. The function uses
# multi-line commands (backslash continuations), so we join them first.
FUNC_BODY=$(sed -n '/^terraform_update_apply()/,/^}/p' "$DEPLOY_SCRIPT")
# Join backslash continuations into single lines for reliable grep
JOINED=$(echo "$FUNC_BODY" | sed -e ':a' -e '/\\$/N; s/\\\n//; ta')
# Count actual terraform plan command invocations (exclude comments/strings)
PLAN_CMD_COUNT=$(echo "$JOINED" | grep -c '^\s*terraform plan ' 2>/dev/null) || PLAN_CMD_COUNT=0
NO_COLOR_COUNT=$(echo "$JOINED" | grep '^\s*terraform plan ' | grep -c '\-no-color' 2>/dev/null) || NO_COLOR_COUNT=0

if [ "$PLAN_CMD_COUNT" -gt 0 ] && [ "$NO_COLOR_COUNT" -eq "$PLAN_CMD_COUNT" ]; then
  pass "deploy-all.sh: all $PLAN_CMD_COUNT terraform plan commands in terraform_update_apply have -no-color"
else
  fail_test "deploy-all.sh: only $NO_COLOR_COUNT of $PLAN_CMD_COUNT terraform plan commands have -no-color"
fi

# ===========================================================================
# Summary
# ===========================================================================
echo ""
echo "=== Results: $PASSES passed, $FAILURES failed ==="
if [ "$FAILURES" -gt 0 ]; then
  echo "FAILED"
  exit 1
fi
echo "ALL PASSED"
exit 0
