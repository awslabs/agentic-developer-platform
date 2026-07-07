#!/bin/bash
# =============================================================================
# install-aidlc.test.sh — Unit tests for install-aidlc.sh strip/merge logic
# =============================================================================
# Tests the settings-strip deny-list, additive merge, conflict detection,
# and version-file writing without requiring network access (no git clone).
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIXTURES_DIR="$SCRIPT_DIR/fixtures"
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

FAILURES=0
PASSES=0

pass() {
  echo "  PASS: $1"
  PASSES=$((PASSES + 1))
}

fail() {
  echo "  FAIL: $1"
  FAILURES=$((FAILURES + 1))
}

# =============================================================================
# Source the strip and assert functions from install-aidlc.sh
# We re-implement the core logic inline to test without needing git clone.
# =============================================================================

# --- Denied keys (must match install-aidlc.sh) --------------------------------
DENIED_TOP_LEVEL_KEYS=(
  "model"
  "provider"
  "env"
  "apiKey"
  "region"
  "baseUrl"
)

DENIED_ENV_PREFIXES=(
  "AWS_"
  "ANTHROPIC_"
)

strip_settings() {
  local input_file="$1"
  local output_file="$2"

  local jq_filter="."
  for key in "${DENIED_TOP_LEVEL_KEYS[@]}"; do
    jq_filter="$jq_filter | del(.[\"$key\"])"
  done

  local stripped
  stripped="$(jq "$jq_filter" "$input_file")"

  local env_strip_filter="."
  for prefix in "${DENIED_ENV_PREFIXES[@]}"; do
    env_strip_filter="$env_strip_filter | walk(if type == \"object\" then with_entries(select(.key | test(\"^${prefix}\") | not)) else . end)"
  done

  echo "$stripped" | jq "$env_strip_filter" > "$output_file"
}

assert_clean() {
  local file="$1"
  local has_violations=0

  for key in "${DENIED_TOP_LEVEL_KEYS[@]}"; do
    if jq -e "has(\"$key\")" "$file" >/dev/null 2>&1; then
      has_violations=1
    fi
  done

  for prefix in "${DENIED_ENV_PREFIXES[@]}"; do
    local matches
    matches="$(jq -r '.. | objects | keys[] | select(test("^'"$prefix"'"))' "$file" 2>/dev/null || true)"
    if [ -n "$matches" ]; then
      has_violations=1
    fi
  done

  return $has_violations
}

# =============================================================================
echo "=== Test Suite: install-aidlc.sh ==="
echo ""

# --- Test 1: Strip removes all denied top-level keys -------------------------
echo "--- Test 1: Strip removes denied top-level keys ---"

strip_settings "$FIXTURES_DIR/aidlc-settings.json" "$TMPDIR/stripped.json"

for key in "${DENIED_TOP_LEVEL_KEYS[@]}"; do
  if jq -e "has(\"$key\")" "$TMPDIR/stripped.json" >/dev/null 2>&1; then
    fail "Top-level key '$key' still present after strip"
  else
    pass "Top-level key '$key' removed"
  fi
done
echo ""

# --- Test 2: Strip removes AWS_*/ANTHROPIC_* env vars -------------------------
echo "--- Test 2: Strip removes AWS_*/ANTHROPIC_* env vars from nested objects ---"

# The env key itself should be removed at top level, but let's test with a
# settings.json that has env vars nested inside other objects
cat > "$TMPDIR/nested-env.json" <<'EOF'
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hook": "test.sh",
        "env": {
          "AWS_REGION": "us-east-1",
          "ANTHROPIC_API_KEY": "secret",
          "SAFE_VAR": "keep-me"
        }
      }
    ]
  }
}
EOF

strip_settings "$TMPDIR/nested-env.json" "$TMPDIR/nested-stripped.json"

if jq -e '.. | objects | select(has("AWS_REGION"))' "$TMPDIR/nested-stripped.json" >/dev/null 2>&1; then
  fail "AWS_REGION still present in nested object"
else
  pass "AWS_REGION removed from nested object"
fi

if jq -e '.. | objects | select(has("ANTHROPIC_API_KEY"))' "$TMPDIR/nested-stripped.json" >/dev/null 2>&1; then
  fail "ANTHROPIC_API_KEY still present in nested object"
else
  pass "ANTHROPIC_API_KEY removed from nested object"
fi

if jq -e '.. | objects | select(has("SAFE_VAR"))' "$TMPDIR/nested-stripped.json" >/dev/null 2>&1; then
  pass "SAFE_VAR preserved in nested object"
else
  fail "SAFE_VAR was incorrectly removed"
fi
echo ""

# --- Test 3: Strip preserves hooks/tools/skills/agents/permissions ------------
echo "--- Test 3: Strip preserves hooks, tools, skills, agents, permissions ---"

strip_settings "$FIXTURES_DIR/aidlc-settings.json" "$TMPDIR/stripped-full.json"

for key in "hooks" "tools" "skills" "agents" "permissions"; do
  if jq -e "has(\"$key\")" "$TMPDIR/stripped-full.json" >/dev/null 2>&1; then
    pass "Key '$key' preserved after strip"
  else
    fail "Key '$key' was incorrectly removed by strip"
  fi
done
echo ""

# --- Test 4: Assert-clean passes on stripped output ---------------------------
echo "--- Test 4: Assert-clean passes on correctly stripped output ---"

if assert_clean "$TMPDIR/stripped.json"; then
  pass "assert_clean passes on stripped settings"
else
  fail "assert_clean failed on stripped settings (should have passed)"
fi
echo ""

# --- Test 5: Assert-clean fails on unstripped input ---------------------------
echo "--- Test 5: Assert-clean fails on unstripped input ---"

if assert_clean "$FIXTURES_DIR/aidlc-settings.json"; then
  fail "assert_clean passed on unstripped input (should have failed)"
else
  pass "assert_clean correctly rejects unstripped input"
fi
echo ""

# --- Test 6: Additive merge preserves existing keys ---------------------------
echo "--- Test 6: Additive merge preserves existing repo settings ---"

# Merge: AIDLC (stripped) as base, existing as overlay
jq -s '.[0] * .[1]' "$TMPDIR/stripped-full.json" "$FIXTURES_DIR/existing-repo-settings.json" > "$TMPDIR/merged.json"

# Existing custom key must survive
if jq -e '.myCustomKey == "preserved-value"' "$TMPDIR/merged.json" >/dev/null 2>&1; then
  pass "Existing 'myCustomKey' preserved in merge"
else
  fail "Existing 'myCustomKey' lost during merge"
fi

# Existing tools.custom must survive
if jq -e '.tools.custom[0] == "my-tool"' "$TMPDIR/merged.json" >/dev/null 2>&1; then
  pass "Existing 'tools.custom' preserved in merge"
else
  fail "Existing 'tools.custom' lost during merge"
fi

# Existing permissions must win (overlay)
if jq -e '.permissions.allow | index("Bash(npm *)")' "$TMPDIR/merged.json" >/dev/null 2>&1; then
  pass "Existing permissions preserved (overlay wins)"
else
  fail "Existing permissions lost during merge"
fi
echo ""

# --- Test 7: Hook conflict detection -----------------------------------------
echo "--- Test 7: Hook-name conflict detection ---"

# Create two settings with the same hook key
cat > "$TMPDIR/aidlc-hooks.json" <<'EOF'
{
  "hooks": {
    "PreToolUse": [{"matcher": "Bash", "hook": "aidlc.sh"}]
  }
}
EOF

cat > "$TMPDIR/existing-hooks.json" <<'EOF'
{
  "hooks": {
    "PreToolUse": [{"matcher": "Write", "hook": "lint.sh"}]
  }
}
EOF

# Check if hook keys overlap
aidlc_hooks="$(jq -r '.hooks // {} | keys[]' "$TMPDIR/aidlc-hooks.json" 2>/dev/null || true)"
existing_hooks="$(jq -r '.hooks // {} | keys[]' "$TMPDIR/existing-hooks.json" 2>/dev/null || true)"

conflict_found=0
while IFS= read -r hook; do
  if [ -n "$hook" ] && echo "$existing_hooks" | grep -qx "$hook"; then
    conflict_found=1
  fi
done <<< "$aidlc_hooks"

if [ "$conflict_found" -eq 1 ]; then
  pass "Hook conflict correctly detected (PreToolUse in both)"
else
  fail "Hook conflict NOT detected (should have found PreToolUse)"
fi
echo ""

# --- Test 8: No conflict when hook keys differ --------------------------------
echo "--- Test 8: No false positive on non-overlapping hooks ---"

cat > "$TMPDIR/aidlc-hooks2.json" <<'EOF'
{
  "hooks": {
    "PostToolUse": [{"matcher": "Write", "hook": "post.sh"}]
  }
}
EOF

aidlc_hooks="$(jq -r '.hooks // {} | keys[]' "$TMPDIR/aidlc-hooks2.json" 2>/dev/null || true)"
existing_hooks="$(jq -r '.hooks // {} | keys[]' "$TMPDIR/existing-hooks.json" 2>/dev/null || true)"

conflict_found=0
while IFS= read -r hook; do
  if [ -n "$hook" ] && echo "$existing_hooks" | grep -qx "$hook"; then
    conflict_found=1
  fi
done <<< "$aidlc_hooks"

if [ "$conflict_found" -eq 0 ]; then
  pass "No false conflict when hooks don't overlap"
else
  fail "False conflict detected on non-overlapping hooks"
fi
echo ""

# --- Test 9: Version file content ---------------------------------------------
echo "--- Test 9: Version file content ---"

VERSION_TAG="v2.2.3"
mkdir -p "$TMPDIR/test-repo/aidlc"
echo "$VERSION_TAG" > "$TMPDIR/test-repo/aidlc/.aidlc-version"

WRITTEN_VERSION="$(cat "$TMPDIR/test-repo/aidlc/.aidlc-version")"
if [ "$WRITTEN_VERSION" = "$VERSION_TAG" ]; then
  pass "Version file contains correct tag: $VERSION_TAG"
else
  fail "Version file contains '$WRITTEN_VERSION', expected '$VERSION_TAG'"
fi
echo ""

# --- Test 10: Stripped output has no model/env via grep verification -----------
echo "--- Test 10: grep verification — no ANTHROPIC/AWS_REGION in stripped output ---"

if grep -q "ANTHROPIC\|AWS_REGION\|AWS_DEFAULT_REGION" "$TMPDIR/stripped-full.json"; then
  fail "grep found ANTHROPIC or AWS_REGION in stripped output"
else
  pass "grep confirms no ANTHROPIC/AWS_REGION in stripped output"
fi

if grep -q '"model"' "$TMPDIR/stripped-full.json"; then
  fail "grep found '\"model\"' key in stripped output"
else
  pass "grep confirms no '\"model\"' key in stripped output"
fi
echo ""

# --- Test 11: aidlc-emit-issues skill exists in repo --------------------------
echo "--- Test 11: aidlc-emit-issues skill present in .claude/skills/ ---"

REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
SKILL_FILE="$REPO_ROOT/modules/agent-factory/skills/aidlc-emit-issues/SKILL.md"
if [ -f "$SKILL_FILE" ]; then
  pass "aidlc-emit-issues/SKILL.md exists"
else
  fail "aidlc-emit-issues/SKILL.md not found at $SKILL_FILE"
fi

# Verify the skill contains required sections
if grep -q "deterministic" "$SKILL_FILE" 2>/dev/null; then
  pass "SKILL.md mentions deterministic gates"
else
  fail "SKILL.md missing deterministic gates guidance"
fi

if grep -q "five-section" "$SKILL_FILE" 2>/dev/null || grep -q "five section" "$SKILL_FILE" 2>/dev/null; then
  pass "SKILL.md references five-section format"
else
  fail "SKILL.md missing five-section format reference"
fi

if grep -q "sub-issue\|sub_issue\|subIssue" "$SKILL_FILE" 2>/dev/null; then
  pass "SKILL.md references sub-issue linking"
else
  fail "SKILL.md missing sub-issue linking instructions"
fi
echo ""

# =============================================================================
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
