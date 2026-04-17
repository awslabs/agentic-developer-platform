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
EXTRA_REPOS=("${@+"$@"}")
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
  # Find the newest .pem file by modification time, regardless of marker
  ls -t "$DOWNLOADS"/*.pem 2>/dev/null | head -1
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
APP_NAMES=("${GITHUB_ORG}-adp-agent-dev" "${GITHUB_ORG}-adp-agent-pm" "${GITHUB_ORG}-adp-agent-ops")
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
    APP_ID="$EXISTING_ID"

    # Check if installed — if not, open browser for installation
    INSTALL_ID=$(gh api "/orgs/$GITHUB_ORG/installations" --jq ".installations[] | select(.app_id==$APP_ID) | .id" 2>/dev/null || echo "")
    if [ -n "$INSTALL_ID" ]; then
      ok "Already installed on $GITHUB_ORG (installation ID: $INSTALL_ID)"
    else
      echo "  App exists but is NOT installed on $GITHUB_ORG. Opening browser..."
      INSTALL_URL="https://github.com/apps/${APP_NAME}/installations/select_target"
      open_url "$INSTALL_URL"
      echo -n "  Press Enter after you've installed the app in the browser..."
      read -r
      sleep 2
      INSTALL_ID=$(gh api "/orgs/$GITHUB_ORG/installations" --jq ".installations[] | select(.app_id==$APP_ID) | .id" 2>/dev/null || echo "")
      [ -n "$INSTALL_ID" ] && ok "Installed (ID: $INSTALL_ID)" || warn "Could not verify installation"
    fi
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
  URL="${URL}&organization_projects=write"
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
  PEM_FILE=$(find_latest_pem)

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

  # Install the app on the org
  echo ""
  echo "  Now install the app on your org."
  echo "  Opening browser — select '$GITHUB_ORG' org, choose 'Only select repositories', pick 'adp'."
  echo ""

  INSTALL_URL="https://github.com/apps/${APP_NAME}/installations/select_target"
  open_url "$INSTALL_URL"

  echo -n "  Press Enter after you've installed the app in the browser..."
  read -r

  # Verify installation and add extra repos
  echo "  Verifying installation..."
  sleep 2
  INSTALL_ID=$(gh api "/orgs/$GITHUB_ORG/installations" --jq ".installations[] | select(.app_id==$APP_ID) | .id" 2>/dev/null || echo "")

  if [ -n "$INSTALL_ID" ]; then
    ok "App installed on $GITHUB_ORG (installation ID: $INSTALL_ID)"

    # Add extra repos if specified
    if [ ${#EXTRA_REPOS[@]+"${#EXTRA_REPOS[@]}"} -gt 0 ] 2>/dev/null; then
      for repo in "${EXTRA_REPOS[@]+"${EXTRA_REPOS[@]}"}"; do
        REPO_ID=$(gh api "/repos/$GITHUB_ORG/$repo" --jq '.id' 2>/dev/null || echo "")
        if [ -n "$REPO_ID" ]; then
          gh api --method PUT "/user/installations/$INSTALL_ID/repositories/$REPO_ID" 2>/dev/null || true
          ok "Added $GITHUB_ORG/$repo to installation"
        fi
      done
    fi
  else
    warn "Could not verify installation. Check https://github.com/organizations/$GITHUB_ORG/settings/installations"
  fi

  echo ""
  ok "App $APP_NAME complete (ID: $APP_ID)"
done

# Summary
echo ""
echo "========================================="
echo -e "${BLUE}Final Validation${NC}"
echo "========================================="
echo ""

ALL_GOOD=true

echo "Secrets:"
for ROLE in dev pm ops; do
  ID=$(aws secretsmanager get-secret-value --secret-id "adp/gh-app-${ROLE}-id" --query 'SecretString' --output text --region "$AWS_REGION" 2>/dev/null || echo "")
  KEY_LEN=$(aws secretsmanager get-secret-value --secret-id "adp/gh-app-${ROLE}-key" --query 'SecretString' --output text --region "$AWS_REGION" 2>/dev/null | wc -c | tr -d ' ')
  if [ -n "$ID" ] && [ "$KEY_LEN" -gt 100 ] 2>/dev/null; then
    ok "adp/gh-app-${ROLE}: ID=$ID, key=${KEY_LEN} chars"
  else
    fail "adp/gh-app-${ROLE}: MISSING or invalid"
    ALL_GOOD=false
  fi
done

echo ""
echo "Installations:"
for i in 0 1 2; do
  ROLE="${APPS[$i]}"
  APP_NAME="${APP_NAMES[$i]}"
  APP_ID=$(aws secretsmanager get-secret-value --secret-id "adp/gh-app-${ROLE}-id" --query 'SecretString' --output text --region "$AWS_REGION" 2>/dev/null || echo "0")
  INSTALL_ID=$(gh api "/orgs/$GITHUB_ORG/installations" --jq ".installations[] | select(.app_id==$APP_ID) | .id" 2>/dev/null || echo "")
  if [ -n "$INSTALL_ID" ]; then
    REPOS=$(gh api "/user/installations/$INSTALL_ID/repositories" --jq '[.repositories[].name] | join(", ")' 2>/dev/null || echo "?")
    ok "$APP_NAME installed (ID: $INSTALL_ID) → repos: $REPOS"
  else
    fail "$APP_NAME NOT installed on $GITHUB_ORG"
    ALL_GOOD=false
  fi
done

echo ""
if [ "$ALL_GOOD" = true ]; then
  echo -e "${GREEN}=========================================${NC}"
  echo -e "${GREEN}All GitHub Apps created and installed${NC}"
  echo -e "${GREEN}=========================================${NC}"
  echo ""
  echo "Next: ./platform/scripts/deploy-all.sh"
else
  echo -e "${RED}=========================================${NC}"
  echo -e "${RED}Some apps are missing or not installed${NC}"
  echo -e "${RED}=========================================${NC}"
  echo ""
  echo "Re-run this script to fix: $0 $GITHUB_ORG"
fi
