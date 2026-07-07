#!/bin/bash
# =============================================================================
# install-aidlc.sh — Install AIDLC v2 Claude Code distribution into a target repo
# =============================================================================
# Clones awslabs/aidlc-workflows at a pinned tag, copies dist/claude/.claude/
# and dist/claude/aidlc/ into the target, strips model/env pins from
# settings.json so they cannot override ADP worker model config, merges
# additively with any existing .claude/settings.json, and commits the result.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Defaults ----------------------------------------------------------------
DEFAULT_TAG="v2.2.3"
AIDLC_REPO="https://github.com/awslabs/aidlc-workflows.git"

# --- Deny-list: top-level keys to strip from settings.json -------------------
# These keys would override ADP's runtime model/provider config if left in.
DENIED_TOP_LEVEL_KEYS=(
  "model"
  "provider"
  "env"
  "apiKey"
  "region"
  "baseUrl"
)

# --- Deny-list: env var prefixes to strip from any "env" object ---------------
DENIED_ENV_PREFIXES=(
  "AWS_"
  "ANTHROPIC_"
)

# --- Usage --------------------------------------------------------------------
usage() {
  cat <<EOF
Usage: $(basename "$0") <target-repo-path> [tag]

Arguments:
  target-repo-path   Path to the target git repository
  tag                AIDLC version tag to install (default: $DEFAULT_TAG)

Example:
  $(basename "$0") /path/to/my-repo v2.2.3
EOF
  exit 1
}

# --- Argument parsing ---------------------------------------------------------
if [ $# -lt 1 ]; then
  usage
fi

TARGET_REPO="$(cd "$1" && pwd)"
TAG="${2:-$DEFAULT_TAG}"

if [ ! -d "$TARGET_REPO/.git" ]; then
  echo "ERROR: $TARGET_REPO is not a git repository."
  exit 1
fi

echo "Installing AIDLC $TAG into $TARGET_REPO"

# --- Clone AIDLC at pinned tag ------------------------------------------------
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

echo "Cloning awslabs/aidlc-workflows at tag $TAG..."
git clone --depth 1 --branch "$TAG" "$AIDLC_REPO" "$TMPDIR/aidlc-workflows" 2>/dev/null || {
  echo "ERROR: Failed to clone $AIDLC_REPO at tag $TAG"
  echo "Ensure the tag exists and you have access to the repository."
  exit 1
}

DIST_DIR="$TMPDIR/aidlc-workflows/dist/claude"

if [ ! -d "$DIST_DIR/.claude" ]; then
  echo "ERROR: dist/claude/.claude/ not found in the cloned repo at tag $TAG"
  exit 1
fi

if [ ! -d "$DIST_DIR/aidlc" ]; then
  echo "ERROR: dist/claude/aidlc/ not found in the cloned repo at tag $TAG"
  exit 1
fi

# --- Strip settings.json ------------------------------------------------------
strip_settings() {
  local input_file="$1"
  local output_file="$2"

  if [ ! -f "$input_file" ]; then
    echo "  No settings.json found in AIDLC dist — skipping strip."
    return 0
  fi

  echo "  Stripping model/env pins from settings.json..."

  # Build jq filter to remove denied top-level keys
  local jq_filter="."
  for key in "${DENIED_TOP_LEVEL_KEYS[@]}"; do
    jq_filter="$jq_filter | del(.[\"$key\"])"
  done

  # Apply top-level key removal
  local stripped
  stripped="$(jq "$jq_filter" "$input_file")"

  # If there's still an "env" key (nested inside other objects), strip AWS_*/ANTHROPIC_* keys
  # Walk the entire JSON and remove any key matching denied env prefixes
  local env_strip_filter="."
  for prefix in "${DENIED_ENV_PREFIXES[@]}"; do
    # Remove from any top-level "env" object that might have survived (e.g. nested)
    env_strip_filter="$env_strip_filter | walk(if type == \"object\" then with_entries(select(.key | test(\"^${prefix}\") | not)) else . end)"
  done

  echo "$stripped" | jq "$env_strip_filter" > "$output_file"
}

# --- Assert no denied keys remain ---------------------------------------------
assert_clean() {
  local file="$1"

  if [ ! -f "$file" ]; then
    return 0
  fi

  local violations=""

  # Check top-level denied keys
  for key in "${DENIED_TOP_LEVEL_KEYS[@]}"; do
    if jq -e "has(\"$key\")" "$file" >/dev/null 2>&1; then
      violations="$violations  - Top-level key '$key' still present\n"
    fi
  done

  # Check for denied env var prefixes anywhere in the JSON
  for prefix in "${DENIED_ENV_PREFIXES[@]}"; do
    local matches
    matches="$(jq -r '.. | objects | keys[] | select(test("^'"$prefix"'"))' "$file" 2>/dev/null || true)"
    if [ -n "$matches" ]; then
      violations="$violations  - Env key prefix '$prefix' found: $matches\n"
    fi
  done

  if [ -n "$violations" ]; then
    echo "ERROR: Strip assertion failed! Denied keys remain in settings.json:"
    echo -e "$violations"
    echo "This is a bug in the strip logic. Aborting."
    exit 1
  fi

  echo "  Strip assertion passed: no denied keys remain."
}

# --- Merge settings.json additively -------------------------------------------
merge_settings() {
  local aidlc_settings="$1"
  local target_settings="$2"
  local output="$3"

  if [ ! -f "$aidlc_settings" ]; then
    # No AIDLC settings to merge
    if [ -f "$target_settings" ]; then
      cp -f "$target_settings" "$output"
    fi
    return 0
  fi

  if [ ! -f "$target_settings" ]; then
    # No existing settings — just use the stripped AIDLC one
    cp -f "$aidlc_settings" "$output"
    return 0
  fi

  echo "  Merging AIDLC settings with existing .claude/settings.json..."

  # Check for hook-name conflicts before merging
  local aidlc_hooks existing_hooks conflicts
  aidlc_hooks="$(jq -r '.hooks // {} | keys[]' "$aidlc_settings" 2>/dev/null || true)"
  existing_hooks="$(jq -r '.hooks // {} | keys[]' "$target_settings" 2>/dev/null || true)"

  if [ -n "$aidlc_hooks" ] && [ -n "$existing_hooks" ]; then
    conflicts=""
    while IFS= read -r hook; do
      if echo "$existing_hooks" | grep -qx "$hook"; then
        conflicts="$conflicts $hook"
      fi
    done <<< "$aidlc_hooks"

    if [ -n "$conflicts" ]; then
      echo "ERROR: Hook-name conflicts detected between AIDLC and existing settings:"
      echo "  Conflicting hooks:$conflicts"
      echo ""
      echo "Resolve these conflicts manually before running install-aidlc.sh again."
      exit 1
    fi
  fi

  # Additive merge: existing keys always win (AIDLC values are base, existing overlay)
  # Use jq '*' (recursive merge) with existing on the right so existing keys are preserved
  jq -s '.[0] * .[1]' "$aidlc_settings" "$target_settings" > "$output"
}

# --- Append CLAUDE.md content -------------------------------------------------
append_claude_md() {
  local aidlc_claude_md="$1"
  local target_claude_md="$2"

  if [ ! -f "$aidlc_claude_md" ]; then
    echo "  No CLAUDE.md found in AIDLC dist — skipping."
    return 0
  fi

  local marker="<!-- BEGIN AIDLC -->"
  local end_marker="<!-- END AIDLC -->"

  if [ -f "$target_claude_md" ] && grep -q "$marker" "$target_claude_md"; then
    echo "  AIDLC section already exists in CLAUDE.md — replacing..."
    # Remove existing AIDLC section and re-add
    sed -i "/$marker/,/$end_marker/d" "$target_claude_md"
  fi

  echo "  Appending AIDLC content to CLAUDE.md..."
  {
    echo ""
    echo "$marker"
    echo "# AIDLC (AI Development Lifecycle) Configuration"
    echo ""
    cat "$aidlc_claude_md"
    echo ""
    echo "$end_marker"
  } >> "${target_claude_md:-$TARGET_REPO/CLAUDE.md}"
}

# --- Main installation flow ---------------------------------------------------
echo ""
echo "=== Phase 1: Strip settings.json ==="
STRIPPED_SETTINGS="$TMPDIR/stripped-settings.json"

strip_settings "$DIST_DIR/.claude/settings.json" "$STRIPPED_SETTINGS"
assert_clean "$STRIPPED_SETTINGS"

echo ""
echo "=== Phase 2: Copy AIDLC distribution ==="

# Copy .claude/ directory contents (but not settings.json yet — we'll merge it)
mkdir -p "$TARGET_REPO/.claude"
echo "  Copying .claude/ contents..."

# Copy everything except settings.json from AIDLC's .claude/
find "$DIST_DIR/.claude" -mindepth 1 -maxdepth 1 -not -name "settings.json" -exec cp -rf {} "$TARGET_REPO/.claude/" \;

# Copy aidlc/ directory
echo "  Copying aidlc/ directory..."
cp -rf "$DIST_DIR/aidlc" "$TARGET_REPO/"

echo ""
echo "=== Phase 3: Merge settings.json ==="
merge_settings "$STRIPPED_SETTINGS" "$TARGET_REPO/.claude/settings.json" "$TMPDIR/merged-settings.json"

if [ -f "$TMPDIR/merged-settings.json" ]; then
  cp -f "$TMPDIR/merged-settings.json" "$TARGET_REPO/.claude/settings.json"
elif [ -f "$STRIPPED_SETTINGS" ]; then
  cp -f "$STRIPPED_SETTINGS" "$TARGET_REPO/.claude/settings.json"
fi

# Final assertion on the installed settings
echo "  Final assertion on installed settings.json..."
assert_clean "$TARGET_REPO/.claude/settings.json"

echo ""
echo "=== Phase 4: Append CLAUDE.md ==="

# Look for CLAUDE.md in the AIDLC dist
AIDLC_CLAUDE_MD=""
if [ -f "$DIST_DIR/CLAUDE.md" ]; then
  AIDLC_CLAUDE_MD="$DIST_DIR/CLAUDE.md"
elif [ -f "$DIST_DIR/.claude/CLAUDE.md" ]; then
  AIDLC_CLAUDE_MD="$DIST_DIR/.claude/CLAUDE.md"
fi

if [ -n "$AIDLC_CLAUDE_MD" ]; then
  append_claude_md "$AIDLC_CLAUDE_MD" "$TARGET_REPO/CLAUDE.md"
else
  echo "  No AIDLC CLAUDE.md found — skipping."
fi

echo ""
echo "=== Phase 5: Copy ADP-owned skills ==="

# The aidlc-emit-issues skill is ADP-owned (not part of upstream AIDLC dist).
# It ships from the tracked source at modules/agent-factory/skills/.
ADP_SKILLS_DIR="$SCRIPT_DIR/../../modules/agent-factory/skills"

if [ -d "$ADP_SKILLS_DIR/aidlc-emit-issues" ]; then
  mkdir -p "$TARGET_REPO/.claude/skills/aidlc-emit-issues"
  cp -rf "$ADP_SKILLS_DIR/aidlc-emit-issues/SKILL.md" "$TARGET_REPO/.claude/skills/aidlc-emit-issues/"
  echo "  Copied aidlc-emit-issues skill to .claude/skills/"
else
  echo "  WARNING: aidlc-emit-issues skill not found at $ADP_SKILLS_DIR/aidlc-emit-issues"
fi

echo ""
echo "=== Phase 5.5: Patch scope docs with gate behavior ==="

# Inject mandatory gate behavior into any installed AIDLC scope docs.
# This ensures the agent sees the gate rule even if reading only the scope file.
GATE_MARKER="<!-- aidlc-gate-behavior-injected -->"
GATE_PATCH="
## Gate behavior (injected by install-aidlc.sh)

Every active stage in this scope gates before advancing. There is no auto-advance
mode. The agent MUST:
1. Execute ONE stage per run
2. Commit aidlc/ state to the work branch
3. Post a gate comment (with \`<!-- aidlc-gate:<stage> -->\` marker)
4. END the run — do not call \`aidlc-state.ts advance\` in the same session

Scope (poc/auto/workshop) controls which stages are active — not whether they
require human approval before advancing.
"

# Patch all .claude/scopes/*.md files that don't already have the marker
if [ -d "$TARGET_REPO/.claude/scopes" ]; then
  for scope_file in "$TARGET_REPO/.claude/scopes"/*.md; do
    [ -f "$scope_file" ] || continue
    if ! grep -q "$GATE_MARKER" "$scope_file"; then
      echo "  Patching gate behavior into: $(basename "$scope_file")"
      {
        echo ""
        echo "$GATE_MARKER"
        echo "$GATE_PATCH"
      } >> "$scope_file"
    else
      echo "  Gate behavior already present in: $(basename "$scope_file") — skipping."
    fi
  done
else
  echo "  No .claude/scopes/ directory found — skipping scope patch."
fi

echo ""
echo "=== Phase 6: Write version file ==="
mkdir -p "$TARGET_REPO/aidlc"
echo "$TAG" > "$TARGET_REPO/aidlc/.aidlc-version"
echo "  Wrote aidlc/.aidlc-version: $TAG"

echo ""
echo "=== Phase 7: Commit ==="
cd "$TARGET_REPO"
git add .claude/ aidlc/
if [ -f "$TARGET_REPO/CLAUDE.md" ]; then
  git add CLAUDE.md
fi
git commit -m "chore: install AIDLC $TAG (ADP-hosted distribution)" || {
  echo "WARNING: Nothing to commit (files may already be up to date)."
}

echo ""
echo "=== Done ==="
echo "AIDLC $TAG installed successfully into $TARGET_REPO"
echo "Version: $(cat "$TARGET_REPO/aidlc/.aidlc-version")"
