#!/bin/bash
# =============================================================================
# install-aidlc.sh — Install AIDLC v2 Claude Code distribution into a target repo
# =============================================================================
# Resolves an AIDLC v2 distribution (the ``dist/claude`` layout: ``.claude/`` +
# ``aidlc/``), copies it into the target, strips model/env pins from
# settings.json so they cannot override ADP worker model config, merges
# additively with any existing .claude/settings.json, and commits the result.
#
# Sources (issue #9539):
#   default               clone awslabs/aidlc-workflows at DEFAULT_TAG (or [tag])
#   --from-git URL[@REF]  clone another git source (a fork, a mirror, a vendored
#                         copy) at REF; the same layout detection applies
#   --from-dir PATH       use a prepared checkout or distribution on disk; no network
#
# The tag is verified with ``git ls-remote`` before anything is cloned and the
# layout is verified before anything is written, so a missing tag or a changed
# upstream layout fails with a precise message and leaves the target untouched.
# Upstream v2.2.1 - v2.2.3 were never published (the release tags jump from
# v2.2.0 to v2.3.0) and from v2.8 upstream moved to a ``harness/<client>``
# layout this installer does not consume; v2.2.0 is what the hosted
# ``@agent-aidlc`` persona was qualified against.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Defaults ----------------------------------------------------------------
# The last published tag of the AIDLC 2.2 line with the dist/claude layout.
DEFAULT_TAG="v2.2.0"
# Overridable so tests and mirrors can point at a local or alternate repository.
AIDLC_REPO="${AIDLC_REPO:-https://github.com/awslabs/aidlc-workflows.git}"

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
  cat <<USAGE
Usage: $(basename "$0") <target-repo-path> [tag] [--from-git URL[@REF] | --from-dir PATH]

Arguments:
  target-repo-path   Path to the target git repository
  tag                AIDLC release tag to install from $AIDLC_REPO
                     (default: $DEFAULT_TAG). Must carry the dist/claude layout
                     (published tags up to v2.7.0 do).

Options:
  --from-git URL[@REF]  Install from another git repository at REF (a fork,
                        mirror or vendored copy with the dist/claude layout, or
                        a repository whose root is already a distribution).
  --from-dir PATH       Install from a checkout or distribution on disk. PATH may
                        be an upstream checkout (contains dist/claude), a
                        distribution root (contains .claude and aidlc), or a
                        repository that already has AIDLC installed.
  -h, --help            Show this help.

Environment:
  AIDLC_REPO            Default git source (currently: $AIDLC_REPO)

Examples:
  $(basename "$0") /path/to/my-repo
  $(basename "$0") /path/to/my-repo v2.3.0
  $(basename "$0") /path/to/my-repo --from-dir /path/to/aidlc-workflows
  $(basename "$0") /path/to/my-repo --from-git https://github.com/org/aidlc-fork.git@v2.2.0
USAGE
  exit "${1:-1}"
}

# --- Argument parsing ---------------------------------------------------------
TARGET_ARG=""
TAG=""
SOURCE_MODE="tag"
SOURCE_GIT=""
SOURCE_REF=""
SOURCE_DIR=""

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --from-git)
      [ $# -ge 2 ] || { echo "ERROR: --from-git requires URL[@REF]"; usage; }
      SOURCE_MODE="git"
      case "$2" in
        *@*) SOURCE_GIT="${2%@*}"; SOURCE_REF="${2##*@}" ;;
        *)   SOURCE_GIT="$2"; SOURCE_REF="" ;;
      esac
      shift 2 ;;
    --from-dir)
      [ $# -ge 2 ] || { echo "ERROR: --from-dir requires PATH"; usage; }
      SOURCE_MODE="dir"
      SOURCE_DIR="$2"
      shift 2 ;;
    --*) echo "ERROR: unknown option $1"; usage ;;
    *)
      if [ -z "$TARGET_ARG" ]; then TARGET_ARG="$1"
      elif [ -z "$TAG" ]; then TAG="$1"
      else echo "ERROR: unexpected argument $1"; usage
      fi
      shift ;;
  esac
done

[ -n "$TARGET_ARG" ] || usage
if [ "$SOURCE_MODE" != "tag" ] && [ -n "$TAG" ]; then
  echo "ERROR: a tag argument cannot be combined with --from-git or --from-dir (use URL@REF for a git ref)."
  exit 1
fi
TAG="${TAG:-$DEFAULT_TAG}"

if [ ! -d "$TARGET_ARG" ]; then
  echo "ERROR: $TARGET_ARG is not a directory."
  exit 1
fi
TARGET_REPO="$(cd "$TARGET_ARG" && pwd)"

if [ ! -d "$TARGET_REPO/.git" ]; then
  echo "ERROR: $TARGET_REPO is not a git repository."
  exit 1
fi

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

# --- Source resolution --------------------------------------------------------
# resolve_dist_dir ROOT LABEL: print the distribution directory inside ROOT, or
# return 1 after naming what was found instead. Nothing is written to the
# target before this succeeds.
resolve_dist_dir() {
  local root="$1" label="$2"
  if [ -d "$root/dist/claude/.claude" ] && [ -d "$root/dist/claude/aidlc" ]; then
    echo "$root/dist/claude"; return 0
  fi
  if [ -d "$root/.claude" ] && [ -d "$root/aidlc" ]; then
    echo "$root"; return 0
  fi
  echo "ERROR: $label does not carry an AIDLC dist/claude distribution." >&2
  if [ -d "$root/harness/claude" ]; then
    echo "  It uses the upstream 'harness/<client>' layout (AIDLC v2.8 and later), which this" >&2
    echo "  installer does not consume. Use a release tag up to v2.7.0 (for example $DEFAULT_TAG)," >&2
    echo "  or --from-dir with a prepared distribution." >&2
  elif [ -d "$root/dist/claude" ]; then
    echo "  dist/claude exists but lacks .claude/ and/or aidlc/." >&2
  else
    echo "  Expected either dist/claude/{.claude,aidlc} or {.claude,aidlc} at its root." >&2
  fi
  return 1
}

case "$SOURCE_MODE" in
  dir)
    if [ ! -d "$SOURCE_DIR" ]; then
      echo "ERROR: --from-dir $SOURCE_DIR does not exist or is not a directory."
      exit 1
    fi
    SOURCE_DIR="$(cd "$SOURCE_DIR" && pwd)"
    DIST_DIR="$(resolve_dist_dir "$SOURCE_DIR" "--from-dir $SOURCE_DIR")" || exit 1
    INSTALLED_VERSION="dir:$(basename "$SOURCE_DIR")"
    if [ -f "$SOURCE_DIR/aidlc/.aidlc-version" ]; then
      INSTALLED_VERSION="$(head -n1 "$SOURCE_DIR/aidlc/.aidlc-version")"
    elif git -C "$SOURCE_DIR" describe --tags --exact-match >/dev/null 2>&1; then
      INSTALLED_VERSION="$(git -C "$SOURCE_DIR" describe --tags --exact-match)"
    fi
    echo "Installing AIDLC ($INSTALLED_VERSION) from $SOURCE_DIR into $TARGET_REPO"
    ;;
  git)
    CLONE_ARGS=(--depth 1)
    [ -n "$SOURCE_REF" ] && CLONE_ARGS+=(--branch "$SOURCE_REF")
    echo "Cloning $SOURCE_GIT${SOURCE_REF:+ at $SOURCE_REF}..."
    if ! git clone "${CLONE_ARGS[@]}" "$SOURCE_GIT" "$TMPDIR/aidlc-source" 2>"$TMPDIR/clone.err"; then
      echo "ERROR: Failed to clone $SOURCE_GIT${SOURCE_REF:+ at ref $SOURCE_REF}."
      sed 's/^/  git: /' "$TMPDIR/clone.err" | head -n 5
      exit 1
    fi
    DIST_DIR="$(resolve_dist_dir "$TMPDIR/aidlc-source" "$SOURCE_GIT${SOURCE_REF:+@$SOURCE_REF}")" || exit 1
    INSTALLED_VERSION="${SOURCE_REF:-$(git -C "$TMPDIR/aidlc-source" rev-parse --short HEAD)}"
    echo "Installing AIDLC $INSTALLED_VERSION from $SOURCE_GIT into $TARGET_REPO"
    ;;
  tag)
    echo "Checking that tag $TAG exists in $AIDLC_REPO..."
    if ! TAG_REF="$(git ls-remote --tags "$AIDLC_REPO" "refs/tags/$TAG" 2>"$TMPDIR/ls-remote.err")"; then
      echo "ERROR: Cannot reach $AIDLC_REPO to verify tag $TAG."
      sed 's/^/  git: /' "$TMPDIR/ls-remote.err" | head -n 5
      exit 1
    fi
    if [ -z "$TAG_REF" ]; then
      echo "ERROR: Tag $TAG does not exist in $AIDLC_REPO."
      echo "  Published release tags can be listed with: git ls-remote --tags $AIDLC_REPO"
      echo "  Tags with the dist/claude layout this installer consumes run up to v2.7.0; the default is $DEFAULT_TAG."
      echo "  To install from a fork, mirror or local checkout use --from-git URL@REF or --from-dir PATH."
      exit 1
    fi
    echo "Cloning $AIDLC_REPO at tag $TAG..."
    if ! git clone --depth 1 --branch "$TAG" "$AIDLC_REPO" "$TMPDIR/aidlc-workflows" 2>"$TMPDIR/clone.err"; then
      echo "ERROR: Failed to clone $AIDLC_REPO at tag $TAG."
      sed 's/^/  git: /' "$TMPDIR/clone.err" | head -n 5
      exit 1
    fi
    DIST_DIR="$(resolve_dist_dir "$TMPDIR/aidlc-workflows" "$AIDLC_REPO at tag $TAG")" || exit 1
    INSTALLED_VERSION="$TAG"
    echo "Installing AIDLC $TAG into $TARGET_REPO"
    ;;
esac

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

Reply options in gate comments MUST be shown mention-prefixed:
\`@agent-aidlc approve\` / \`@agent-aidlc feedback: [notes]\` / \`@agent-aidlc skip\`

Replies without the \`@agent-aidlc\` mention are not seen by the webhook
(mention-routed dispatch). The gate comment must state this explicitly.

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
echo "=== Phase 5.8: Copy AIDLC intent issue template ==="

# The aidlc-intent.yml issue template provides the structured form for starting
# AIDLC inception. Without it, users must hand-apply the aidlc-intent label.
TEMPLATE_SOURCE="$SCRIPT_DIR/../../.github/ISSUE_TEMPLATE/aidlc-intent.yml"
TEMPLATE_DEST="$TARGET_REPO/.github/ISSUE_TEMPLATE/aidlc-intent.yml"

if [ ! -f "$TEMPLATE_SOURCE" ]; then
  echo "  WARNING: Source template not found at $TEMPLATE_SOURCE — skipping."
elif [ -f "$TEMPLATE_DEST" ]; then
  echo "  aidlc-intent.yml already exists in target repo — skipping (no clobber)."
else
  mkdir -p "$TARGET_REPO/.github/ISSUE_TEMPLATE"
  cp -f "$TEMPLATE_SOURCE" "$TEMPLATE_DEST"
  echo "  Copied aidlc-intent.yml issue template to .github/ISSUE_TEMPLATE/"
fi

echo ""
echo "=== Phase 6: Write version file ==="
mkdir -p "$TARGET_REPO/aidlc"
echo "$INSTALLED_VERSION" > "$TARGET_REPO/aidlc/.aidlc-version"
echo "  Wrote aidlc/.aidlc-version: $INSTALLED_VERSION"

echo ""
echo "=== Phase 7: Commit ==="
cd "$TARGET_REPO"
git add .claude/ aidlc/
if [ -f "$TARGET_REPO/CLAUDE.md" ]; then
  git add CLAUDE.md
fi
if [ -d "$TARGET_REPO/.github/ISSUE_TEMPLATE" ]; then
  git add .github/ISSUE_TEMPLATE/
fi
git commit -m "chore: install AIDLC $INSTALLED_VERSION (ADP-hosted distribution)" || {
  echo "WARNING: Nothing to commit (files may already be up to date)."
}

echo ""
echo "=== Done ==="
echo "AIDLC $INSTALLED_VERSION installed successfully into $TARGET_REPO"
echo "Version: $(cat "$TARGET_REPO/aidlc/.aidlc-version")"
