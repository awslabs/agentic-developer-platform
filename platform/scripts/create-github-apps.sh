#!/bin/bash
set -euo pipefail

# =============================================================================
# ADP — Create GitHub Apps for Agent Factory
# =============================================================================
# Creates 3 GitHub Apps (DEV, PM, OPS) using the manifest flow, installs them
# on the ADP repo and any additional repos, and stores credentials in
# AWS Secrets Manager.
#
# Usage:
#   ./platform/scripts/create-github-apps.sh <github-org> [repo1 repo2 ...]
#
# Example:
#   ./platform/scripts/create-github-apps.sh acme-corp
#   ./platform/scripts/create-github-apps.sh acme-corp frontend-app backend-api
# =============================================================================

GITHUB_ORG="${1:-}"
shift || true
EXTRA_REPOS=("$@")
AWS_REGION="${AWS_REGION:-us-east-1}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓ $1${NC}"; }
warn() { echo -e "${YELLOW}⚠ $1${NC}"; }
fail() { echo -e "${RED}✗ $1${NC}"; exit 1; }

if [ -z "$GITHUB_ORG" ]; then
  echo "Usage: $0 <github-org> [repo1 repo2 ...]"
  echo ""
  echo "Creates 3 GitHub Apps for ADP Agent Factory:"
  echo "  - adp-agent-dev  (developer + architect agents)"
  echo "  - adp-agent-pm   (PM agent)"
  echo "  - adp-agent-ops  (reviewer + operations agents)"
  echo ""
  echo "Apps are installed on the 'adp' repo by default."
  echo "Pass additional repo names to install on those too."
  exit 1
fi

command -v gh &>/dev/null || fail "GitHub CLI (gh) not installed"
command -v aws &>/dev/null || fail "AWS CLI not installed"
gh auth status &>/dev/null || fail "GitHub CLI not authenticated. Run: gh auth login"

# The permissions each agent app needs
APP_PERMISSIONS='{
  "contents": "write",
  "issues": "write",
  "pull_requests": "write",
  "workflows": "write",
  "metadata": "read",
  "members": "read"
}'

# App definitions: name, slug, secrets-manager prefix
declare -A APPS
APPS[dev]="adp-agent-dev"
APPS[pm]="adp-agent-pm"
APPS[ops]="adp-agent-ops"

SECRETS_PREFIX="adp/gh-app"

echo ""
echo "ADP GitHub App Setup for org: $GITHUB_ORG"
echo "==========================================="
echo ""

# =============================================================================
# Step 1: Create apps via manifest flow
# =============================================================================
for role in dev pm ops; do
  APP_NAME="${APPS[$role]}"
  echo -e "${BLUE}── Creating app: $APP_NAME ──${NC}"

  # Check if app already exists
  EXISTING_APP_ID=$(gh api "/orgs/$GITHUB_ORG/installations" --jq ".installations[] | select(.app_slug==\"$APP_NAME\") | .app_id" 2>/dev/null || echo "")

  if [ -n "$EXISTING_APP_ID" ]; then
    ok "App $APP_NAME already exists (ID: $EXISTING_APP_ID)"
    eval "APP_ID_${role}=$EXISTING_APP_ID"
    continue
  fi

  # Create manifest
  MANIFEST=$(cat <<EOF
{
  "name": "$APP_NAME",
  "url": "https://github.com/$GITHUB_ORG/adp",
  "hook_attributes": {"active": false},
  "public": false,
  "default_permissions": {
    "contents": "write",
    "issues": "write",
    "pull_requests": "write",
    "workflows": "write",
    "metadata": "read",
    "members": "read"
  },
  "default_events": ["issues", "pull_request"]
}
EOF
)

  echo "Opening browser for app creation approval..."
  echo "Please click 'Create GitHub App' in the browser window."
  echo ""

  # Create a temporary manifest file
  MANIFEST_FILE="/tmp/adp-app-manifest-${role}.json"
  echo "$MANIFEST" > "$MANIFEST_FILE"

  # Use the manifest flow — this opens a browser
  # The user clicks approve, GitHub redirects with a code
  CREATION_URL="https://github.com/organizations/$GITHUB_ORG/settings/apps/new"

  echo "  Manifest URL: $CREATION_URL"
  echo ""
  echo "  Copy this JSON manifest and paste it in the GitHub App creation form:"
  echo "  (Or use the 'App Manifests' tab if available)"
  echo ""
  cat "$MANIFEST_FILE"
  echo ""
  echo ""

  # Open browser
  if command -v open &>/dev/null; then
    open "$CREATION_URL"
  elif command -v xdg-open &>/dev/null; then
    xdg-open "$CREATION_URL"
  fi

  echo "After creating the app in the browser, enter the App ID:"
  read -r APP_ID
  echo "Enter the path to the downloaded private key .pem file:"
  read -r PEM_PATH

  if [ ! -f "$PEM_PATH" ]; then
    fail "Private key file not found: $PEM_PATH"
  fi

  eval "APP_ID_${role}=$APP_ID"
  eval "PEM_PATH_${role}=$PEM_PATH"

  ok "App $APP_NAME created (ID: $APP_ID)"
  rm -f "$MANIFEST_FILE"
done

# =============================================================================
# Step 2: Store credentials in AWS Secrets Manager
# =============================================================================
echo ""
echo -e "${BLUE}── Storing credentials in Secrets Manager ──${NC}"

for role in dev pm ops; do
  APP_ID_VAR="APP_ID_${role}"
  PEM_VAR="PEM_PATH_${role}"
  APP_ID="${!APP_ID_VAR}"
  PEM_PATH="${!PEM_VAR:-}"

  # Store App ID
  SECRET_ID="${SECRETS_PREFIX}-${role}-id"
  if aws secretsmanager describe-secret --secret-id "$SECRET_ID" --region "$AWS_REGION" &>/dev/null; then
    aws secretsmanager put-secret-value --secret-id "$SECRET_ID" --secret-string "$APP_ID" --region "$AWS_REGION" > /dev/null
    ok "Updated $SECRET_ID"
  else
    aws secretsmanager create-secret --name "$SECRET_ID" --secret-string "$APP_ID" --region "$AWS_REGION" > /dev/null
    ok "Created $SECRET_ID"
  fi

  # Store Private Key (if we have it)
  if [ -n "$PEM_PATH" ] && [ -f "$PEM_PATH" ]; then
    SECRET_KEY="${SECRETS_PREFIX}-${role}-key"
    PEM_CONTENT=$(cat "$PEM_PATH")
    if aws secretsmanager describe-secret --secret-id "$SECRET_KEY" --region "$AWS_REGION" &>/dev/null; then
      aws secretsmanager put-secret-value --secret-id "$SECRET_KEY" --secret-string "$PEM_CONTENT" --region "$AWS_REGION" > /dev/null
      ok "Updated $SECRET_KEY"
    else
      aws secretsmanager create-secret --name "$SECRET_KEY" --secret-string "$PEM_CONTENT" --region "$AWS_REGION" > /dev/null
      ok "Created $SECRET_KEY"
    fi
  fi
done

# =============================================================================
# Step 3: Install apps on repos
# =============================================================================
echo ""
echo -e "${BLUE}── Installing apps on repositories ──${NC}"

# Always install on the adp repo
ALL_REPOS=("adp" "${EXTRA_REPOS[@]}")

for role in dev pm ops; do
  APP_NAME="${APPS[$role]}"
  APP_ID_VAR="APP_ID_${role}"
  APP_ID="${!APP_ID_VAR}"

  # Get installation ID for this app
  INSTALL_ID=$(gh api "/orgs/$GITHUB_ORG/installations" --jq ".installations[] | select(.app_id==$APP_ID) | .id" 2>/dev/null || echo "")

  if [ -z "$INSTALL_ID" ]; then
    warn "App $APP_NAME not yet installed on org. Install it manually:"
    warn "  https://github.com/organizations/$GITHUB_ORG/settings/installations"
    continue
  fi

  for repo in "${ALL_REPOS[@]}"; do
    # Get repo ID
    REPO_ID=$(gh api "/repos/$GITHUB_ORG/$repo" --jq '.id' 2>/dev/null || echo "")
    if [ -z "$REPO_ID" ]; then
      warn "Repo $GITHUB_ORG/$repo not found, skipping"
      continue
    fi

    # Add repo to installation
    HTTP_STATUS=$(gh api --method PUT "/user/installations/$INSTALL_ID/repositories/$REPO_ID" 2>&1 || true)
    ok "Installed $APP_NAME on $GITHUB_ORG/$repo"
  done
done

# =============================================================================
# Summary
# =============================================================================
echo ""
echo "==========================================="
echo -e "${GREEN}GitHub App setup complete${NC}"
echo "==========================================="
echo ""
echo "Apps created:"
echo "  DEV (developer + architect): ID ${APP_ID_dev}"
echo "  PM  (project manager):       ID ${APP_ID_pm}"
echo "  OPS (reviewer + operations): ID ${APP_ID_ops}"
echo ""
echo "Credentials stored in Secrets Manager:"
echo "  ${SECRETS_PREFIX}-dev-id, ${SECRETS_PREFIX}-dev-key"
echo "  ${SECRETS_PREFIX}-pm-id,  ${SECRETS_PREFIX}-pm-key"
echo "  ${SECRETS_PREFIX}-ops-id, ${SECRETS_PREFIX}-ops-key"
echo ""
echo "Installed on repos:"
for repo in "${ALL_REPOS[@]}"; do
  echo "  $GITHUB_ORG/$repo"
done
echo ""
echo "Next: run ./platform/scripts/deploy-all.sh --agent-factory-only"
