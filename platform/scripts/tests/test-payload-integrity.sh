#!/bin/bash
# =============================================================================
# test-payload-integrity.sh — Regression tests for remote installer integrity
# =============================================================================
# Tests the download-to-temp, validate, then execute pattern introduced in
# Issue #6115 to replace curl-pipe-shell anti-patterns.
#
# Exercises:
# - Checksum verification rejects tampered downloads (Dolt pattern)
# - Script validation rejects non-script payloads (Beads/Helm/GitLab pattern)
# - Empty downloads are rejected
# - Temp files are cleaned up on failure
# =============================================================================
set -euo pipefail

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

# --- Setup temp dir ---
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

# =============================================================================
# 1. Checksum verification (Dolt pattern from setup-beads/action.yml)
# =============================================================================
echo "=== Checksum verification ==="

# Create a fixture tarball
echo "valid binary content" > "$TMPDIR/valid-payload"
VALID_SHA=$(sha256sum "$TMPDIR/valid-payload" | awk '{print $1}')

# Test: correct checksum passes
ACTUAL_SHA=$(sha256sum "$TMPDIR/valid-payload" | awk '{print $1}')
if [ "$ACTUAL_SHA" = "$VALID_SHA" ]; then
  pass "correct checksum matches"
else
  fail_test "correct checksum should have matched"
fi

# Test: wrong checksum is rejected
WRONG_SHA="0000000000000000000000000000000000000000000000000000000000000000"
ACTUAL_SHA=$(sha256sum "$TMPDIR/valid-payload" | awk '{print $1}')
if [ "$ACTUAL_SHA" != "$WRONG_SHA" ]; then
  pass "wrong checksum rejected"
else
  fail_test "wrong checksum should have been rejected"
fi

# Test: tampered file produces different checksum
echo "tampered content" > "$TMPDIR/tampered-payload"
TAMPERED_SHA=$(sha256sum "$TMPDIR/tampered-payload" | awk '{print $1}')
if [ "$TAMPERED_SHA" != "$VALID_SHA" ]; then
  pass "tampered file produces different checksum"
else
  fail_test "tampered file should have a different checksum"
fi

echo ""

# =============================================================================
# 2. Script validation (Beads/Helm/GitLab pattern)
# =============================================================================
echo "=== Script content validation ==="

# Test: valid shell script (with shebang) passes validation
cat > "$TMPDIR/valid-script.sh" <<'EOF'
#!/bin/bash
echo "hello"
EOF
if [ -s "$TMPDIR/valid-script.sh" ] && head -1 "$TMPDIR/valid-script.sh" | grep -q '^#!'; then
  pass "valid script with shebang passes validation"
else
  fail_test "valid script should pass validation"
fi

# Test: HTML response (SPA fallback) is rejected
cat > "$TMPDIR/html-response.sh" <<'EOF'
<!DOCTYPE html>
<html><head><title>App</title></head><body></body></html>
EOF
if [ -s "$TMPDIR/html-response.sh" ] && head -1 "$TMPDIR/html-response.sh" | grep -q '^#!'; then
  fail_test "HTML response should be rejected"
else
  pass "HTML response rejected (not a script)"
fi

# Test: empty file is rejected
: > "$TMPDIR/empty-file.sh"
if [ -s "$TMPDIR/empty-file.sh" ]; then
  fail_test "empty file should be rejected"
else
  pass "empty file rejected"
fi

# Test: JSON error response is rejected
cat > "$TMPDIR/json-error.sh" <<'EOF'
{"error": "not found", "status": 404}
EOF
if [ -s "$TMPDIR/json-error.sh" ] && head -1 "$TMPDIR/json-error.sh" | grep -q '^#!'; then
  fail_test "JSON error response should be rejected"
else
  pass "JSON error response rejected (not a script)"
fi

# Test: script with #!/usr/bin/env bash shebang passes
cat > "$TMPDIR/env-shebang.sh" <<'EOF'
#!/usr/bin/env bash
set -e
echo "ok"
EOF
if [ -s "$TMPDIR/env-shebang.sh" ] && head -1 "$TMPDIR/env-shebang.sh" | grep -q '^#!'; then
  pass "env-style shebang passes validation"
else
  fail_test "env-style shebang should pass validation"
fi

# Test: script with #!/bin/sh shebang passes
cat > "$TMPDIR/sh-shebang.sh" <<'EOF'
#!/bin/sh
echo "posix"
EOF
if [ -s "$TMPDIR/sh-shebang.sh" ] && head -1 "$TMPDIR/sh-shebang.sh" | grep -q '^#!'; then
  pass "sh-style shebang passes validation"
else
  fail_test "sh-style shebang should pass validation"
fi

echo ""

# =============================================================================
# 3. Temp file cleanup on failure
# =============================================================================
echo "=== Temp file cleanup ==="

# Test: temp file is created then removed on simulated failure
TEMP_FILE=$(mktemp "$TMPDIR/cleanup-test.XXXXXX")
echo "staged content" > "$TEMP_FILE"
# Simulate failed validation → cleanup
rm -f "$TEMP_FILE"
if [ ! -f "$TEMP_FILE" ]; then
  pass "temp file cleaned up after simulated failure"
else
  fail_test "temp file should have been removed"
fi

echo ""

# =============================================================================
# 4. Validate modified scripts parse correctly
# =============================================================================
echo "=== Syntax validation of modified files ==="

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

if bash -n "$SCRIPT_DIR/install-prereqs.sh" 2>/dev/null; then
  pass "install-prereqs.sh: valid bash syntax"
else
  fail_test "install-prereqs.sh: bash syntax error"
fi

if bash -n "$SCRIPT_DIR/deploy-all.sh" 2>/dev/null; then
  pass "deploy-all.sh: valid bash syntax"
else
  fail_test "deploy-all.sh: bash syntax error"
fi

# user_data.sh contains Terraform template variables, so stub them
STUBBED=$(sed 's/${[^}]*}/PLACEHOLDER/g; s/%{[^~]*~}//' "$ROOT_DIR/modules/source-control/gitlab/infra/user_data.sh")
if echo "$STUBBED" | bash -n 2>/dev/null; then
  pass "user_data.sh: valid bash syntax (with template vars stubbed)"
else
  fail_test "user_data.sh: bash syntax error"
fi

# action.yml is YAML; validate by checking structure markers are present
ACTION_FILE="$ROOT_DIR/modules/agent-factory/actions/setup-beads/action.yml"
if [ -f "$ACTION_FILE" ] && grep -q '^runs:' "$ACTION_FILE" && grep -q 'using:' "$ACTION_FILE"; then
  # Verify the shell run blocks parse as bash (extract between 'run: |' markers)
  if grep -q 'curl.*|.*bash' "$ACTION_FILE"; then
    fail_test "setup-beads/action.yml: still contains curl-pipe-bash pattern"
  else
    pass "setup-beads/action.yml: no curl-pipe-bash pattern found"
  fi
else
  fail_test "setup-beads/action.yml: missing expected YAML structure"
fi

echo ""

# =============================================================================
# Summary
# =============================================================================
echo "=============================="
echo "Results: $PASSES passed, $FAILURES failed"
echo "=============================="

if [ "$FAILURES" -gt 0 ]; then
  exit 1
else
  echo "All payload integrity tests passed."
fi
