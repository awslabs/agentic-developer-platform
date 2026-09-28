#!/bin/bash
# =============================================================================
# test-flip-gate-check.sh — Unit tests for flip-gate-check.sh exit codes
# =============================================================================
# Verifies that the gate script exits non-zero when ANY gate fails, and exits 0
# only when ALL gates pass. Uses mock aws/gh CLIs to simulate each failure mode.
#
# Issue: #3605 — gate script exit code was swallowed by `| tee` without pipefail
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_DIR="$(mktemp -d)"
FAILURES=0
PASSES=0

cleanup() {
  rm -rf "$TEST_DIR"
}
trap cleanup EXIT

# -------------------------------------------------------------------
# Mock setup — create fake aws and gh CLIs in a temp PATH directory
# -------------------------------------------------------------------
MOCK_BIN="$TEST_DIR/bin"
mkdir -p "$MOCK_BIN"

# Default mock behaviors (all gates pass)
setup_mocks_all_pass() {
  # aws mock: returns empty datapoints for CloudWatch, "true" for SSM
  cat > "$MOCK_BIN/aws" << 'MOCK_AWS'
#!/bin/bash
case "$*" in
  *get-metric-statistics*)
    echo '{"Datapoints":[]}'
    ;;
  *get-parameter*)
    echo "true"
    ;;
  *)
    echo "{}"
    ;;
esac
exit 0
MOCK_AWS
  chmod +x "$MOCK_BIN/aws"

  # gh mock: returns successful nightly run
  # Note: the real `gh run list --jq '.[0]'` outputs a single JSON object, not an array
  cat > "$MOCK_BIN/gh" << 'MOCK_GH'
#!/bin/bash
case "$*" in
  *"run list"*)
    echo '{"conclusion":"success","createdAt":"2026-07-10T00:00:00Z","databaseId":12345}'
    ;;
  *)
    echo "[]"
    ;;
esac
exit 0
MOCK_GH
  chmod +x "$MOCK_BIN/gh"

  # bc mock: numeric comparison
  cat > "$MOCK_BIN/bc" << 'MOCK_BC'
#!/bin/bash
# Evaluate the expression passed via stdin or args
# We only need to handle "X > 0" comparisons
input="${1:-$(cat)}"
case "$input" in
  "0 > 0") echo "0" ;;
  *) echo "0" ;;
esac
MOCK_BC
  chmod +x "$MOCK_BIN/bc"

  # jq is needed — use the real one if available, otherwise mock
  if command -v jq &>/dev/null; then
    ln -sf "$(command -v jq)" "$MOCK_BIN/jq"
  else
    cat > "$MOCK_BIN/jq" << 'MOCK_JQ'
#!/bin/bash
# Minimal jq mock for the patterns used in flip-gate-check.sh
input="$(cat)"
case "$1" in
  "[.Datapoints[].Sum] | add // 0")
    echo "0"
    ;;
  ".[0]")
    echo "$input" | sed 's/^\[//;s/\]$//'
    ;;
  -r)
    case "$2" in
      ".conclusion") echo "success" ;;
      ".createdAt") echo "2026-07-10T00:00:00Z" ;;
      ".databaseId") echo "12345" ;;
    esac
    ;;
esac
MOCK_JQ
    chmod +x "$MOCK_BIN/jq"
  fi

  # date mock
  cat > "$MOCK_BIN/date" << 'MOCK_DATE'
#!/bin/bash
echo "2026-07-11T12:00:00Z"
MOCK_DATE
  chmod +x "$MOCK_BIN/date"
}

# Override to make Gate 1 fail (drift > 0)
setup_mock_gate1_fail() {
  cat > "$MOCK_BIN/aws" << 'MOCK_AWS'
#!/bin/bash
case "$*" in
  *CredentialAuthorizationDrift*)
    echo '{"Datapoints":[{"Sum":3.0}]}'
    ;;
  *CredentialAuthorizationFallback*)
    echo '{"Datapoints":[]}'
    ;;
  *get-parameter*)
    echo "true"
    ;;
  *)
    echo "{}"
    ;;
esac
exit 0
MOCK_AWS
  chmod +x "$MOCK_BIN/aws"

  # bc needs to return "1" for "3.0 > 0"
  cat > "$MOCK_BIN/bc" << 'MOCK_BC'
#!/bin/bash
input="${1:-$(cat)}"
case "$input" in
  "0 > 0") echo "0" ;;
  *) echo "1" ;;
esac
MOCK_BC
  chmod +x "$MOCK_BIN/bc"
}

# Override to make Gate 2 fail (fallback events > 0)
setup_mock_gate2_fail() {
  cat > "$MOCK_BIN/aws" << 'MOCK_AWS'
#!/bin/bash
case "$*" in
  *CredentialAuthorizationDrift*)
    echo '{"Datapoints":[]}'
    ;;
  *CredentialAuthorizationFallback*)
    echo '{"Datapoints":[{"Sum":5.0}]}'
    ;;
  *get-parameter*)
    echo "true"
    ;;
  *)
    echo "{}"
    ;;
esac
exit 0
MOCK_AWS
  chmod +x "$MOCK_BIN/aws"

  cat > "$MOCK_BIN/bc" << 'MOCK_BC'
#!/bin/bash
input="${1:-$(cat)}"
case "$input" in
  "0 > 0") echo "0" ;;
  *) echo "1" ;;
esac
MOCK_BC
  chmod +x "$MOCK_BIN/bc"
}

# Override to make Gate 3 fail (nightly E2E red)
setup_mock_gate3_fail() {
  cat > "$MOCK_BIN/gh" << 'MOCK_GH'
#!/bin/bash
case "$*" in
  *"run list"*)
    echo '{"conclusion":"failure","createdAt":"2026-07-10T00:00:00Z","databaseId":12345}'
    ;;
  *)
    echo "[]"
    ;;
esac
exit 0
MOCK_GH
  chmod +x "$MOCK_BIN/gh"
}

# Override to make Gate 4 fail (sandbox param not true)
setup_mock_gate4_fail() {
  cat > "$MOCK_BIN/aws" << 'MOCK_AWS'
#!/bin/bash
case "$*" in
  *get-metric-statistics*)
    echo '{"Datapoints":[]}'
    ;;
  *get-parameter*)
    echo "false"
    ;;
  *)
    echo "{}"
    ;;
esac
exit 0
MOCK_AWS
  chmod +x "$MOCK_BIN/aws"
}

# -------------------------------------------------------------------
# Test runner
# -------------------------------------------------------------------
run_gate_script() {
  # Run with mocked PATH; capture exit code
  local exit_code=0
  PATH="$MOCK_BIN:$PATH" \
  GITHUB_REPOSITORY="test-org/test-repo" \
    "$SCRIPT_DIR/flip-gate-check.sh" \
      --environment dev \
      --window-days 7 \
      --sandbox-tenant adp-security-test \
      --aws-region us-east-1 \
      --repo "test-org/test-repo" \
      > "$TEST_DIR/output.txt" 2>&1 || exit_code=$?
  echo "$exit_code"
}

assert_exit_zero() {
  local description="$1"
  local actual_exit="$2"
  if [ "$actual_exit" -eq 0 ]; then
    echo "  PASS: $description (exit 0)"
    PASSES=$((PASSES + 1))
  else
    echo "  FAIL: $description — expected exit 0, got exit $actual_exit"
    echo "        Output: $(tail -5 "$TEST_DIR/output.txt")"
    FAILURES=$((FAILURES + 1))
  fi
}

assert_exit_nonzero() {
  local description="$1"
  local actual_exit="$2"
  if [ "$actual_exit" -ne 0 ]; then
    echo "  PASS: $description (exit $actual_exit)"
    PASSES=$((PASSES + 1))
  else
    echo "  FAIL: $description — expected non-zero exit, got exit 0"
    echo "        Output: $(tail -5 "$TEST_DIR/output.txt")"
    FAILURES=$((FAILURES + 1))
  fi
}

# -------------------------------------------------------------------
# Tests
# -------------------------------------------------------------------
echo "=== Testing flip-gate-check.sh exit codes ==="
echo ""

# Test: all gates pass → exit 0
echo "--- All gates pass ---"
setup_mocks_all_pass
EXIT_CODE=$(run_gate_script)
assert_exit_zero "All 4 gates pass → script exits 0" "$EXIT_CODE"
echo ""

# Test: Gate 1 fails (drift > 0) → exit non-zero
echo "--- Gate 1 fails (drift metric > 0) ---"
setup_mocks_all_pass
setup_mock_gate1_fail
EXIT_CODE=$(run_gate_script)
assert_exit_nonzero "Gate 1 fail (drift > 0) → script exits non-zero" "$EXIT_CODE"
echo ""

# Test: Gate 2 fails (fallback events > 0) → exit non-zero
echo "--- Gate 2 fails (fallback events > 0) ---"
setup_mocks_all_pass
setup_mock_gate2_fail
EXIT_CODE=$(run_gate_script)
assert_exit_nonzero "Gate 2 fail (fallback > 0) → script exits non-zero" "$EXIT_CODE"
echo ""

# Test: Gate 3 fails (nightly E2E red) → exit non-zero
echo "--- Gate 3 fails (nightly E2E red) ---"
setup_mocks_all_pass
setup_mock_gate3_fail
EXIT_CODE=$(run_gate_script)
assert_exit_nonzero "Gate 3 fail (E2E conclusion=failure) → script exits non-zero" "$EXIT_CODE"
echo ""

# Test: Gate 4 fails (sandbox param not true) → exit non-zero
echo "--- Gate 4 fails (sandbox param = false) ---"
setup_mocks_all_pass
setup_mock_gate4_fail
EXIT_CODE=$(run_gate_script)
assert_exit_nonzero "Gate 4 fail (ENABLE_USER_CREDENTIALS=false) → script exits non-zero" "$EXIT_CODE"
echo ""

# Test: Multiple gates fail → exit non-zero
echo "--- Multiple gates fail (Gate 1 + Gate 3) ---"
setup_mocks_all_pass
setup_mock_gate1_fail
setup_mock_gate3_fail
EXIT_CODE=$(run_gate_script)
assert_exit_nonzero "Gates 1+3 fail → script exits non-zero" "$EXIT_CODE"
echo ""

# -------------------------------------------------------------------
# Summary
# -------------------------------------------------------------------
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
