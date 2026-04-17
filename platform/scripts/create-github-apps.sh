#!/bin/bash
set -euo pipefail

# =============================================================================
# ADP — Create GitHub Apps (Interactive)
# =============================================================================
# Opens browser for each app, waits for user to approve, finds the downloaded
# private key, and stores everything in AWS Secrets Manager.
#
# Usage:
#   ./platform/scripts/create-github-apps.sh <github-org> [repo1 repo2 ...]
# =============================================================================

GITHUB_ORG="${1:-}"
shift 2>/dev/null || true
EXTRA_REPOS=("${@}")
AWS_REGION="${AWS_REGION:-us-east-1}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}⚠${NC} $1"; }
fail() { echo -e "${RED}✗${NC} $1"; exit 1; }

if [ -z "$GITHUB_ORG" ]; then
  echo "Usage: $0 <github-org> [extra-repo1 extra-repo2 ...]"
  echo ""
  echo "Creates 3 GitHub Apps for ADP Agent Factory:"
  echo "  adp-agent-dev  (developer + architect)"
  echo "  adp-agent-pm   (project manager)"
  echo "  adp-agent-ops  (reviewer + operations)"
  echo ""
  echo "Opens browser for each app. You click 'Create', then 'Generate a private key'."
  echo "The script finds the downloaded .pem and stores it in Secrets Manager."
  exit 1
fi

command -v aws &>/dev/null || fail "AWS CLI not installed"

# Detect Downloads folder
if [ -d "$HOME/Downloads" ]; then
  DOWNLOADS="$HOME/Downloads"
elif [ -d "$HOME/download" ]; then
  DOWNLOADS="$HOME/download"
else
  DOWNLOADS="/tmp"
fi

# Open browser helper
open_url() {
  local url="$1"
  if command -v open &>/dev/null; then
    open "$url"
  elif command -v xdg-open &>/dev/null; then
    xdg-open "$url"
  elif command -v wslview &>/dev/null; then
    wslview "$url"
  else
    echo "  Open this URL in your browser:"
    echo "  $url"
  fi
}

# Find the most recent .pem file in Downloads
find_latest_pem() {
  local since="$1"
  # Look for .pem files modified after $since timestamp
  find "$DOWNLOADS" -maxdepth 1 -name "*.pem" -newer "$since" -type f 2>/dev/null | head -1
}

# Store a secret in Secrets Manager (create or update)
store_secret() {
  local name="$1"
  local value="$2"
  if aws secretsmanager describe-secret --secret-id "$name" --region "$AWS_REGION" &>/dev/null; then
    aws secretsmanager put-secret-value --secret-id "$name" --secret-string "$value" --region "$AWS_REGION" > /dev/null
  else
    aws secretsmanager create-secret --name "$name" --secret-string "$value" --region "$AWS_REGION" > /dev/null
  fi
}

echo ""
echo "ADP GitHub App Creator"
echo "======================"
echo "Org: $GITHUB_ORG"
echo ""

APPS=("dev" "pm" "ops")
APP_NAMES=("adp-agent-dev" "adp-agent-pm" "adp-agent-ops")
APP_DESCRIPTIONS=("Developer + Architect agents" "Project Manager agent" "Reviewer + Operations agents")

for i in 0 1 2; do
  ROLE="${APPS[$i]}"
  APP_NAME="${APP_NAMES[$i]}"
  APP_DESC="${APP_DESCRIPTIONS[$i]}"

  echo ""
  echo -e "${BLUE}━━━ App $((i+1))/3: $APP_NAME ($APP_DESC) ━━━${NC}"
  echo ""

  # Check if already exists in Secrets Manager
  EXISTING_ID=$(aws secretsmanager get-secret-value --secret-id "adp/gh-app-${ROLE}-id" --query 'SecretString' --output text --region "$AWS_REGION" 2>/dev/null || echo "")
  EXISTING_KEY=$(aws secretsmanager get-secret-value --secret-id "adp/gh-app-${ROLE}-key" --query 'SecretString' --output text --region "$AWS_REGION" 2>/dev/null || echo "")

  if [ -n "$EXISTING_ID" ] && [ -n "$EXISTING_KEY" ] && [ "$EXISTING_KEY" != "" ]; then
    ok "App $APP_NAME already configured (ID: $EXISTING_ID)"
    echo "  Skipping. Delete the secrets to recreate."
    continue
  fi

  # Build the manifest URL with pre-filled permissions
  URL="https://github.com/organizations/${GITHUB_ORG}/settings/apps/new"
  URL="${URL}?name=${APP_NAME}"
  URL="${URL}&url=https://github.com/${GITHUB_ORG}/adp"
  URL="${URL}&public=false"
  URL="${URL}&webhook_active=false"
  URL="${URL}&contents=write"
  URL="${URL}&issues=write"
  URL="${URL}&pull_requests=write"
  URL="${URL}&workflows=write"
  URL="${URL}&metadata=read"
  URL="${URL}&members=read"
  URL="${URL}&events[]=issues"
  URL="${URL}&events[]=pull_request"

  # Create a timestamp marker to find new .pem files
  MARKER=$(mktemp)

  echo "  Opening browser to create $APP_NAME..."
  echo ""
  echo "  In the browser:"
  echo "    1. Click 'Create GitHub App'"
  echo "    2. Note the App ID shown at the top"
  echo "    3. Scroll down → 'Generate a private key' → downloads a .pem file"
  echo ""

  open_url "$URL"

  # Wait for user to enter the App ID
  echo -n "  Enter the App ID: "
  read -r APP_ID

  if [ -z "$APP_ID" ]; then
    fail "No App ID provided"
  fi

  # Wait a moment for the .pem download to complete
  echo "  Looking for the private key .pem file..."
  sleep 2

  PEM_FILE=""
  # Try to find it automatically
  PEM_FILE=$(find_latest_pem "$MARKER")

  if [ -z "$PEM_FILE" ]; then
    # Ask user
    echo ""
    echo "  Could not auto-detect the .pem file."
    echo -n "  Enter the path to the downloaded .pem file: "
    read -r PEM_FILE
  fi

  rm -f "$MARKER"

  if [ ! -f "$PEM_FILE" ]; then
    fail "Private key file not found: $PEM_FILE"
  fi

  ok "Found private key: $PEM_FILE"

  # Store in Secrets Manager
  echo "  Storing credentials in Secrets Manager..."
  store_secret "adp/gh-app-${ROLE}-id" "$APP_ID"
  store_secret "adp/gh-app-${ROLE}-key" "$(cat "$PEM_FILE")"
  ok "Stored adp/gh-app-${ROLE}-id and adp/gh-app-${ROLE}-key"

  # Install the app on repos
  echo "  Installing app on repos..."
  INSTALL_ID=$(gh api "/orgs/$GITHUB_ORG/installations" --jq ".installations[] | select(.app_id==$APP_ID) | .id" 2>/dev/null || echo "")

  if [ -n "$INSTALL_ID" ]; then
    ALL_REPOS=("adp" "${EXTRA_REPOS[@]}")
    for repo in "${ALL_REPOS[@]}"; do
      REPO_ID=$(gh api "/repos/$GITHUB_ORG/$repo" --jq '.id' 2>/dev/null || echo "")
      if [ -n "$REPO_ID" ]; then
        gh api --method PUT "/user/installations/$INSTALL_ID/repositories/$REPO_ID" 2>/dev/null || true
        ok "Installed on $GITHUB_ORG/$repo"
      fi
    done
  else
    warn "App not yet installed on org. Install it manually at:"
    warn "  https://github.com/organizations/$GITHUB_ORG/settings/installations"
  fi

  echo ""
  ok "App $APP_NAME complete (ID: $APP_ID)"
done

# Summary
echo ""
echo "========================================="
echo -e "${GREEN}All GitHub Apps created${NC}"
echo "========================================="
echo ""
echo "Secrets stored:"
for ROLE in dev pm ops; do
  ID=$(aws secretsmanager get-secret-value --secret-id "adp/gh-app-${ROLE}-id" --query 'SecretString' --output text --region "$AWS_REGION" 2>/dev/null || echo "?")
  echo "  adp/gh-app-${ROLE}-id  = $ID"
  echo "  adp/gh-app-${ROLE}-key = (stored)"
done
echo ""
echo "Next: ./platform/scripts/deploy-all.sh"
