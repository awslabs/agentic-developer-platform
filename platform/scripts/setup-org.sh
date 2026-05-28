#!/bin/bash
set -euo pipefail

# =============================================================================
# ADP — Setup Repository in Your GitHub Organization
# =============================================================================
# Forks or pushes the ADP repo to your GitHub org and updates all workflow
# references so Agent Factory works with your org.
#
# Usage:
#   ./platform/scripts/setup-org.sh <github-org> [repo-name]
#   ./platform/scripts/setup-org.sh my-company              # creates my-company/adp
#   ./platform/scripts/setup-org.sh my-company platform-adp # creates my-company/platform-adp
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓ $1${NC}"; }
warn() { echo -e "${YELLOW}⚠ $1${NC}"; }
fail() { echo -e "${RED}✗ $1${NC}"; exit 1; }

# Validate args
GITHUB_ORG="${1:-}"
REPO_NAME="${2:-adp}"
if [ -z "$GITHUB_ORG" ]; then
  echo "Usage: $0 <github-org> [repo-name]"
  echo ""
  echo "Examples:"
  echo "  $0 my-company              # creates my-company/adp"
  echo "  $0 my-company platform-adp # creates my-company/platform-adp"
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

if gh repo view "$GITHUB_ORG/$REPO_NAME" &>/dev/null; then
  ok "Repository $GITHUB_ORG/$REPO_NAME already exists"

  # Check if current remote points to the right place
  CURRENT_REMOTE=$(git -C "$ROOT_DIR" remote get-url origin 2>/dev/null || echo "")
  if echo "$CURRENT_REMOTE" | grep -q "$GITHUB_ORG/$REPO_NAME"; then
    ok "Git remote already points to $GITHUB_ORG/$REPO_NAME"
  else
    echo "Updating git remote to $GITHUB_ORG/$REPO_NAME..."
    git -C "$ROOT_DIR" remote set-url origin "https://github.com/$GITHUB_ORG/$REPO_NAME.git"
    ok "Git remote updated"
  fi
else
  echo "Creating repository $GITHUB_ORG/$REPO_NAME..."
  gh repo create "$GITHUB_ORG/$REPO_NAME" --private --source="$ROOT_DIR" --push
  ok "Repository created and code pushed to $GITHUB_ORG/$REPO_NAME"
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
      sed -i '' "s|aws-e/adp|$GITHUB_ORG/$REPO_NAME|g" "$f" 2>/dev/null || \
      sed -i "s|aws-e/adp|$GITHUB_ORG/$REPO_NAME|g" "$f"
      UPDATED=$((UPDATED+1))
    fi
  fi
done

# Client workflows
for f in "$ROOT_DIR"/modules/agent-factory/client-workflows/.github/workflows/*.yml; do
  if [ -f "$f" ]; then
    if grep -q "aws-e/adp" "$f" 2>/dev/null; then
      sed -i '' "s|aws-e/adp|$GITHUB_ORG/$REPO_NAME|g" "$f" 2>/dev/null || \
      sed -i "s|aws-e/adp|$GITHUB_ORG/$REPO_NAME|g" "$f"
      UPDATED=$((UPDATED+1))
    fi
  fi
done

# AGENTS.md and docs
for f in "$ROOT_DIR"/AGENTS.md "$ROOT_DIR"/CLAUDE.md "$ROOT_DIR"/.kiro/steering/deployment.md; do
  if [ -f "$f" ]; then
    if grep -q "aws-e/adp" "$f" 2>/dev/null; then
      sed -i '' "s|aws-e/adp|$GITHUB_ORG/$REPO_NAME|g" "$f" 2>/dev/null || \
      sed -i "s|aws-e/adp|$GITHUB_ORG/$REPO_NAME|g" "$f"
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
# Step 2.5: Create config/deployment.yml
# =============================================================================
echo ""
echo -e "${BLUE}Step 2.5: Deployment config${NC}"

CONFIG_FILE="$ROOT_DIR/config/deployment.yml"
if [ -f "$CONFIG_FILE" ]; then
  ok "config/deployment.yml already exists — leaving in place. Edit manually if you need to change account/region/environment."
else
  # Try to detect AWS account so the config defaults are useful out of the box.
  DETECTED_ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo "")
  DETECTED_REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || echo us-east-1)}"

  mkdir -p "$ROOT_DIR/config"
  cat > "$CONFIG_FILE" <<EOF
# Generated by setup-org.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)
# Edit values as needed. See config/deployment.yml.example for full schema.
account_id: "${DETECTED_ACCOUNT}"
region: ${DETECTED_REGION}
environment: dev
github_org: ${GITHUB_ORG}

# Optional: cross-account deploy (uncomment + fill in to deploy ADP into a
# linked customer account via the gateway's credential-assume-role endpoint).
# customer_account:
#   account_id: ""
#   aws_label: ""
EOF
  ok "Created config/deployment.yml (account=${DETECTED_ACCOUNT:-<unset>}, region=$DETECTED_REGION, org=$GITHUB_ORG)"
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
to use $GITHUB_ORG/$REPO_NAME instead of aws-e/adp."
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

ACTIONS_ENABLED=$(gh api "repos/$GITHUB_ORG/$REPO_NAME/actions/permissions" --jq '.enabled' 2>/dev/null || echo "unknown")
if [ "$ACTIONS_ENABLED" = "true" ]; then
  ok "GitHub Actions is enabled for $GITHUB_ORG/$REPO_NAME"
else
  warn "Could not verify GitHub Actions status. Ensure Actions is enabled in repo Settings → Actions → General."
fi

# =============================================================================
# Step 5: Create agent trigger labels
# =============================================================================
# Each agent workflow is triggered by applying its matching label to an issue.
# Created idempotently — existing labels are updated with the canonical color/description.
echo ""
echo -e "${BLUE}Step 5: Create agent trigger labels${NC}"

# Labels derived from .github/workflows/agent-*.yml `github.event.label.name == 'X'` clauses.
# name|color|description
AGENT_LABELS=(
  "agent-architect|5319e7|Triggers the architect agent to produce a design doc"
  "agent-developer|1d76db|Triggers the developer agent to implement the issue"
  "agent-operations|fbca04|Triggers the operations agent for infra/ops work"
  "agent-pm|0e8a16|Triggers the PM agent to plan/decompose the issue"
  "agent-product|b60205|Triggers the product agent for scoping and requirements"
  "agent-pt-superpower|5319e7|Triggers the PT superpower agent"
  "agent-reviewer|d93f0b|Triggers the reviewer agent on PRs/issues"
)

LABELS_CREATED=0
LABELS_UPDATED=0
for entry in "${AGENT_LABELS[@]}"; do
  IFS='|' read -r LNAME LCOLOR LDESC <<< "$entry"
  if gh api "repos/$GITHUB_ORG/$REPO_NAME/labels/$LNAME" &>/dev/null; then
    gh api --method PATCH "repos/$GITHUB_ORG/$REPO_NAME/labels/$LNAME" \
      -f new_name="$LNAME" -f color="$LCOLOR" -f description="$LDESC" &>/dev/null || true
    LABELS_UPDATED=$((LABELS_UPDATED+1))
  else
    gh api --method POST "repos/$GITHUB_ORG/$REPO_NAME/labels" \
      -f name="$LNAME" -f color="$LCOLOR" -f description="$LDESC" &>/dev/null && \
      LABELS_CREATED=$((LABELS_CREATED+1)) || \
      warn "Could not create label $LNAME (check repo permissions)"
  fi
done

if [ "$LABELS_CREATED" -gt 0 ] || [ "$LABELS_UPDATED" -gt 0 ]; then
  ok "Agent labels: $LABELS_CREATED created, $LABELS_UPDATED updated on $GITHUB_ORG/$REPO_NAME"
fi

# =============================================================================
# Summary
# =============================================================================
echo ""
echo "========================================="
echo -e "${GREEN}Setup complete for $GITHUB_ORG/$REPO_NAME${NC}"
echo "========================================="
echo ""
echo "Next steps:"
echo "  1. Run preflight:  ./platform/scripts/preflight-check.sh"
echo "  2. Deploy:         ./platform/scripts/deploy-all.sh"
echo ""
echo "For Agent Factory, you'll also need to:"
echo "  - Create GitHub Apps (see modules/agent-factory/SETUP-GUIDE.md)"
echo "  - Store credentials in AWS Secrets Manager"
