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

# The canonical template is a plain-terms opening plus five technical sections
# (six in total); accept the wording the skill and the issue-authoring guide use.
if grep -q -E "five-section|five section|five technical sections|six sections" "$SKILL_FILE" 2>/dev/null; then
  pass "SKILL.md references the plain-terms + five-section issue format"
else
  fail "SKILL.md missing the issue-format reference (five technical sections / six sections)"
fi

if grep -q "sub-issue\|sub_issue\|subIssue" "$SKILL_FILE" 2>/dev/null; then
  pass "SKILL.md references sub-issue linking"
else
  fail "SKILL.md missing sub-issue linking instructions"
fi
echo ""

# =============================================================================
# End-to-end: run the REAL installer against temporary git repositories (#9539).
#
# No network: the default-tag path is exercised against a local bare repository
# through AIDLC_REPO, and --from-dir against a generated distribution tree. The
# installer must succeed with a valid source and must fail with a precise
# message — writing nothing to the target — for a missing tag or a foreign
# layout (upstream's harness/<client> layout from v2.8 on).
# =============================================================================
INSTALLER="$SCRIPT_DIR/../install-aidlc.sh"

make_dist() {
  # make_dist ROOT: write a minimal dist/claude distribution under ROOT.
  local root="$1"
  mkdir -p "$root/dist/claude/.claude/agents" "$root/dist/claude/.claude/skills/aidlc" \
           "$root/dist/claude/.claude/scopes" "$root/dist/claude/aidlc/spaces"
  echo "# agent" > "$root/dist/claude/.claude/agents/aidlc.md"
  echo "# skill" > "$root/dist/claude/.claude/skills/aidlc/SKILL.md"
  echo "# feature scope" > "$root/dist/claude/.claude/scopes/aidlc-feature.md"
  echo "# AIDLC" > "$root/dist/claude/.claude/CLAUDE.md"
  echo "placeholder" > "$root/dist/claude/aidlc/spaces/.keep"
  cat > "$root/dist/claude/.claude/settings.json" <<'SETTINGS'
{
  "model": "claude-opus-4",
  "env": {"AWS_REGION": "us-east-1", "ANTHROPIC_API_KEY": "x"},
  "hooks": {"PreToolUse": [{"matcher": "Bash", "hook": "guard.sh"}]},
  "permissions": {"allow": ["Read"]}
}
SETTINGS
}

make_target() {
  # make_target DIR: an empty git repository with an identity so commits work in CI.
  local dir="$1"
  mkdir -p "$dir"
  git -C "$dir" init -q
  git -C "$dir" config user.email "ci@example.invalid"
  git -C "$dir" config user.name "CI"
}

assert_installed() {
  # assert_installed TARGET LABEL VERSION: the AC-01 tree and strip assertions.
  local target="$1" label="$2" version="$3"
  [ -d "$target/.claude/agents" ] && pass "$label: .claude/agents installed" || fail "$label: .claude/agents missing"
  [ -d "$target/.claude/skills/aidlc" ] && pass "$label: .claude/skills installed" || fail "$label: .claude/skills missing"
  [ -d "$target/aidlc" ] && pass "$label: aidlc/ installed" || fail "$label: aidlc/ missing"
  if [ -f "$target/.claude/settings.json" ] && assert_clean "$target/.claude/settings.json"; then
    pass "$label: settings.json has no model/provider/env keys"
  else
    fail "$label: settings.json still carries denied keys (or is missing)"
  fi
  if jq -e '.hooks.PreToolUse' "$target/.claude/settings.json" >/dev/null 2>&1; then
    pass "$label: non-denied settings (hooks) preserved"
  else
    fail "$label: hooks dropped from settings.json"
  fi
  if [ "$(cat "$target/aidlc/.aidlc-version" 2>/dev/null)" = "$version" ]; then
    pass "$label: aidlc/.aidlc-version is $version"
  else
    fail "$label: aidlc/.aidlc-version is '$(cat "$target/aidlc/.aidlc-version" 2>/dev/null)', expected $version"
  fi
  if grep -q "aidlc-gate-behavior-injected" "$target/.claude/scopes/aidlc-feature.md" 2>/dev/null; then
    pass "$label: gate behaviour patched into scope doc"
  else
    fail "$label: scope doc not patched with gate behaviour"
  fi
  if git -C "$target" log -1 --format=%s 2>/dev/null | grep -q "install AIDLC $version"; then
    pass "$label: installation committed"
  else
    fail "$label: no installation commit (last: $(git -C "$target" log -1 --format=%s 2>/dev/null))"
  fi
  if ! grep -q "ERROR" "$target.log"; then
    pass "$label: no ERROR lines in installer output"
  else
    fail "$label: installer printed ERROR: $(grep -m1 ERROR "$target.log")"
  fi
}

assert_untouched() {
  # assert_untouched TARGET LABEL: AC-02 — a refused install writes nothing.
  local target="$1" label="$2"
  if [ ! -e "$target/.claude" ] && [ ! -e "$target/aidlc" ] && [ -z "$(git -C "$target" log --oneline 2>/dev/null)" ]; then
    pass "$label: target left untouched"
  else
    fail "$label: target was modified despite the refusal"
  fi
}

# --- Test 13: AC-01 — default tag path against a local source repository -------
echo "--- Test 13: default tag path installs from the configured source (AC-01) ---"
make_dist "$TMPDIR/source-work"
git -C "$TMPDIR/source-work" init -q
git -C "$TMPDIR/source-work" config user.email "ci@example.invalid"
git -C "$TMPDIR/source-work" config user.name "CI"
git -C "$TMPDIR/source-work" add -A
git -C "$TMPDIR/source-work" commit -q -m "dist"
git -C "$TMPDIR/source-work" tag v2.2.0
git clone -q --bare "$TMPDIR/source-work" "$TMPDIR/source.git"
make_target "$TMPDIR/target-default"
if AIDLC_REPO="file://$TMPDIR/source.git" bash "$INSTALLER" "$TMPDIR/target-default" > "$TMPDIR/target-default.log" 2>&1; then
  pass "default-tag install exits 0"
else
  fail "default-tag install exited non-zero: $(tail -n 3 "$TMPDIR/target-default.log" | tr '\n' ' ')"
fi
assert_installed "$TMPDIR/target-default" "default-tag" "v2.2.0"
echo ""

# --- Test 14: AC-01 — --from-dir on an upstream-style checkout -----------------
echo "--- Test 14: --from-dir installs from a checkout without network (AC-01) ---"
make_target "$TMPDIR/target-dir"
if bash "$INSTALLER" "$TMPDIR/target-dir" --from-dir "$TMPDIR/source-work" > "$TMPDIR/target-dir.log" 2>&1; then
  pass "--from-dir install exits 0"
else
  fail "--from-dir install exited non-zero: $(tail -n 3 "$TMPDIR/target-dir.log" | tr '\n' ' ')"
fi
assert_installed "$TMPDIR/target-dir" "--from-dir" "v2.2.0"
echo ""

# --- Test 15: AC-01 — --from-git URL@REF ---------------------------------------
echo "--- Test 15: --from-git URL@REF installs from an alternate repository (AC-01) ---"
make_target "$TMPDIR/target-git"
if bash "$INSTALLER" "$TMPDIR/target-git" --from-git "file://$TMPDIR/source.git@v2.2.0" > "$TMPDIR/target-git.log" 2>&1; then
  pass "--from-git install exits 0"
else
  fail "--from-git install exited non-zero: $(tail -n 3 "$TMPDIR/target-git.log" | tr '\n' ' ')"
fi
assert_installed "$TMPDIR/target-git" "--from-git" "v2.2.0"
echo ""

# --- Test 16: AC-02 — a tag that does not exist is refused precisely ----------
echo "--- Test 16: missing tag is refused by name and writes nothing (AC-02) ---"
make_target "$TMPDIR/target-missing-tag"
if AIDLC_REPO="file://$TMPDIR/source.git" bash "$INSTALLER" "$TMPDIR/target-missing-tag" v2.2.3 > "$TMPDIR/target-missing-tag.log" 2>&1; then
  fail "missing tag: installer exited 0"
else
  pass "missing tag: installer exits non-zero"
fi
if grep -q "Tag v2.2.3 does not exist in file://$TMPDIR/source.git" "$TMPDIR/target-missing-tag.log"; then
  pass "missing tag: message names the tag and the repository"
else
  fail "missing tag: message imprecise: $(grep -m1 ERROR "$TMPDIR/target-missing-tag.log")"
fi
assert_untouched "$TMPDIR/target-missing-tag" "missing tag"
echo ""

# --- Test 17: AC-02 — the upstream harness layout is named, not mis-installed --
echo "--- Test 17: harness/<client> layout is refused with guidance (AC-02) ---"
mkdir -p "$TMPDIR/harness-src/harness/claude/skills"
make_target "$TMPDIR/target-harness"
if bash "$INSTALLER" "$TMPDIR/target-harness" --from-dir "$TMPDIR/harness-src" > "$TMPDIR/target-harness.log" 2>&1; then
  fail "harness layout: installer exited 0"
else
  pass "harness layout: installer exits non-zero"
fi
if grep -q "harness/<client>" "$TMPDIR/target-harness.log" && grep -q "v2.7.0" "$TMPDIR/target-harness.log"; then
  pass "harness layout: message names the layout and the supported tag range"
else
  fail "harness layout: message imprecise: $(grep -m2 -i "error\|harness" "$TMPDIR/target-harness.log" | tr '\n' ' ')"
fi
assert_untouched "$TMPDIR/target-harness" "harness layout"
echo ""

# --- Test 18: AC-02 — a missing --from-dir path and a non-repository target ---
echo "--- Test 18: bad source path and non-git target are refused (AC-02) ---"
make_target "$TMPDIR/target-badpath"
if bash "$INSTALLER" "$TMPDIR/target-badpath" --from-dir "$TMPDIR/does-not-exist" > "$TMPDIR/target-badpath.log" 2>&1; then
  fail "missing --from-dir path: installer exited 0"
else
  pass "missing --from-dir path: installer exits non-zero"
fi
grep -q "does not exist or is not a directory" "$TMPDIR/target-badpath.log" && pass "missing --from-dir path: message names the path" || fail "missing --from-dir path: imprecise message"
assert_untouched "$TMPDIR/target-badpath" "missing --from-dir path"
mkdir -p "$TMPDIR/not-a-repo"
if bash "$INSTALLER" "$TMPDIR/not-a-repo" --from-dir "$TMPDIR/source-work" > "$TMPDIR/not-a-repo.log" 2>&1; then
  fail "non-git target: installer exited 0"
else
  pass "non-git target: installer exits non-zero"
fi
grep -q "is not a git repository" "$TMPDIR/not-a-repo.log" && pass "non-git target: message says so" || fail "non-git target: imprecise message"
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
