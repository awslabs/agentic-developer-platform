#!/bin/bash
set -euo pipefail

# =============================================================================
# ADP — Setup Repository in Your GitHub Organization
# =============================================================================
# Forks or pushes the ADP repo to your GitHub org and updates all workflow
# references so Agent Factory works with your org.
#
# Usage:
#   ./platform/scripts/setup-org.sh <github-org>
#   ./platform/scripts/setup-org.sh my-company
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓ $1${NC}"; }
warn() { echo -e "${YELLOW}⚠ $1${NC}"; }
fail() { echo -e "${RED}✗ $1${NC}"; exit 1; }

# Validate args
GITHUB_ORG="${1:-}"
if [ -z "$GITHUB_ORG" ]; then
  echo "Usage: $0 <github-org>"
  echo ""
  echo "Example: $0 my-company"
  echo ""
  echo "This script:"
  echo "  1. Creates the adp repo in your GitHub org (if it doesn't exist)"
  echo "  2. Updates all agent workflow references to your org"
  echo "  3. Commits and pushes the changes"
  exit 1
fi

# Check prerequisites
command -v gh &>/dev/null || fail "GitHub CLI (gh) not installed. Install: https://cli.github.com/"
gh auth status &>/dev/null || fail "GitHub CLI not authenticated. Run: gh auth login"

echo ""
echo "Setting up ADP for GitHub org: $GITHUB_ORG"
echo ""

# =============================================================================
# Step 1: Ensure repo exists in the org
# =============================================================================
echo -e "${BLUE}Step 1: Repository setup${NC}"

if gh repo view "$GITHUB_ORG/adp" &>/dev/null; then
  ok "Repository $GITHUB_ORG/adp already exists"

  # Check if current remote points to the right place
  CURRENT_REMOTE=$(git -C "$ROOT_DIR" remote get-url origin 2>/dev/null || echo "")
  if echo "$CURRENT_REMOTE" | grep -q "$GITHUB_ORG/adp"; then
    ok "Git remote already points to $GITHUB_ORG/adp"
  else
    echo "Updating git remote to $GITHUB_ORG/adp..."
    git -C "$ROOT_DIR" remote set-url origin "https://github.com/$GITHUB_ORG/adp.git"
    ok "Git remote updated"
  fi
else
  echo "Creating repository $GITHUB_ORG/adp..."
  gh repo create "$GITHUB_ORG/adp" --private --source="$ROOT_DIR" --push
  ok "Repository created and code pushed to $GITHUB_ORG/adp"
fi

# =============================================================================
# Step 2: Update workflow references
# =============================================================================
echo ""
echo -e "${BLUE}Step 2: Updating workflow references${NC}"

UPDATED=0

# Agent workflows in .github/workflows/
for f in "$ROOT_DIR"/.github/workflows/agent-*.yml \
         "$ROOT_DIR"/.github/workflows/pr-review-trigger.yml \
         "$ROOT_DIR"/.github/workflows/skill-agent.yml; do
  if [ -f "$f" ]; then
    if grep -q "aws-e/adp" "$f" 2>/dev/null; then
      sed -i '' "s|aws-e/adp|$GITHUB_ORG/adp|g" "$f" 2>/dev/null || \
      sed -i "s|aws-e/adp|$GITHUB_ORG/adp|g" "$f"
      UPDATED=$((UPDATED+1))
    fi
  fi
done

# Client workflows
for f in "$ROOT_DIR"/modules/agent-factory/client-workflows/.github/workflows/*.yml; do
  if [ -f "$f" ]; then
    if grep -q "aws-e/adp" "$f" 2>/dev/null; then
      sed -i '' "s|aws-e/adp|$GITHUB_ORG/adp|g" "$f" 2>/dev/null || \
      sed -i "s|aws-e/adp|$GITHUB_ORG/adp|g" "$f"
      UPDATED=$((UPDATED+1))
    fi
  fi
done

# AGENTS.md and docs
for f in "$ROOT_DIR"/AGENTS.md "$ROOT_DIR"/CLAUDE.md "$ROOT_DIR"/.kiro/steering/deployment.md; do
  if [ -f "$f" ]; then
    if grep -q "aws-e/adp" "$f" 2>/dev/null; then
      sed -i '' "s|aws-e/adp|$GITHUB_ORG/adp|g" "$f" 2>/dev/null || \
      sed -i "s|aws-e/adp|$GITHUB_ORG/adp|g" "$f"
      UPDATED=$((UPDATED+1))
    fi
  fi
done

# deploy-all.sh default github_org
if [ -f "$ROOT_DIR/platform/scripts/deploy-all.sh" ]; then
  if grep -q 'github_org       = "aws-e"' "$ROOT_DIR/platform/scripts/deploy-all.sh" 2>/dev/null; then
    sed -i '' "s|github_org       = \"aws-e\"|github_org       = \"$GITHUB_ORG\"|g" "$ROOT_DIR/platform/scripts/deploy-all.sh" 2>/dev/null || \
    sed -i "s|github_org       = \"aws-e\"|github_org       = \"$GITHUB_ORG\"|g" "$ROOT_DIR/platform/scripts/deploy-all.sh"
    UPDATED=$((UPDATED+1))
  fi
fi

if [ "$UPDATED" -gt 0 ]; then
  ok "Updated $UPDATED files with org: $GITHUB_ORG"
else
  ok "All files already reference $GITHUB_ORG (no changes needed)"
fi

# =============================================================================
# Step 3: Commit and push
# =============================================================================
echo ""
echo -e "${BLUE}Step 3: Commit and push${NC}"

cd "$ROOT_DIR"
if [ -n "$(git status --porcelain)" ]; then
  git add -A
  git commit -m "chore: configure ADP for GitHub org $GITHUB_ORG

Updated workflow references, client workflows, docs, and deploy script
to use $GITHUB_ORG/adp instead of aws-e/adp."
  git push
  ok "Changes committed and pushed"
else
  ok "No changes to commit (already configured)"
fi

# =============================================================================
# Step 4: Verify GitHub Actions is enabled
# =============================================================================
echo ""
echo -e "${BLUE}Step 4: Verify GitHub Actions${NC}"

ACTIONS_ENABLED=$(gh api "repos/$GITHUB_ORG/adp/actions/permissions" --jq '.enabled' 2>/dev/null || echo "unknown")
if [ "$ACTIONS_ENABLED" = "true" ]; then
  ok "GitHub Actions is enabled for $GITHUB_ORG/adp"
else
  warn "Could not verify GitHub Actions status. Ensure Actions is enabled in repo Settings → Actions → General."
fi

# =============================================================================
# Summary
# =============================================================================
echo ""
echo "========================================="
echo -e "${GREEN}Setup complete for $GITHUB_ORG/adp${NC}"
echo "========================================="
echo ""
echo "Next steps:"
echo "  1. Run preflight:  ./platform/scripts/preflight-check.sh"
echo "  2. Deploy:         ./platform/scripts/deploy-all.sh"
echo ""
echo "For Agent Factory, you'll also need to:"
echo "  - Create GitHub Apps (see modules/agent-factory/SETUP-GUIDE.md)"
echo "  - Store credentials in AWS Secrets Manager"
