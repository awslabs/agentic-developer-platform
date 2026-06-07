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
  echo "Creates 3 GitHub Apps + 1 GitHub OAuth App for ADP:"
  echo "  GitHub Apps (server-to-server auth for agents):"
  echo "    adp-agent-dev  (developer + architect)"
  echo "    adp-agent-pm   (project manager)"
  echo "    adp-agent-ops  (reviewer + operations)"
  echo "  GitHub OAuth App (user login on the SPA):"
  echo "    adp-<org>     (used by the auth broker — adp/<env>/cognito/github-oauth-credentials)"
  echo ""
  echo "Opens browser for each. You click 'Create' / 'Register', paste IDs back,"
  echo "the script finds the downloaded .pem (for GitHub Apps) and stores everything"
  echo "in AWS Secrets Manager."
  exit 1
fi

# Optional environment for the OAuth App secret path (default dev).
# OAuth App is per-environment because callback URL differs (dev/staging/prod
# each have their own API Gateway invoke URL).
ADP_ENV="${ADP_ENV:-dev}"

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
echo "Owner/org arg: $GITHUB_ORG"
echo ""

# -----------------------------------------------------------------------------
# App ownership scope — org-owned vs user-owned
# -----------------------------------------------------------------------------
# GitHub Apps are owned by EITHER an organization (requires org-owner rights) OR
# a user account (requires only that the user can admin the target repo). The
# ARC runner registers at the repo level either way (githubConfigUrl uses the
# repo), so the only thing that changes between the two is WHERE the app is
# created and HOW its installation is verified.
#
# Set APP_SCOPE=org|user to skip the prompt (useful for automation).
APP_SCOPE="${APP_SCOPE:-}"
if [ -z "$APP_SCOPE" ]; then
  echo "Where should the GitHub Apps be created/owned?"
  echo "  1) Organization  — apps owned by the '$GITHUB_ORG' org."
  echo "                     Requires you to be an ORG OWNER."
  echo "  2) Personal/repo — apps owned by your GitHub user account, then installed"
  echo "                     on specific repo(s). Works with only REPO ADMIN access"
  echo "                     (no org-owner rights needed) — for corporate setups."
  echo -n "Choose [1/2] (default 1): "
  read -r _scope_choice
  case "${_scope_choice:-1}" in
    2) APP_SCOPE="user" ;;
    *) APP_SCOPE="org" ;;
  esac
fi
[ "$APP_SCOPE" = "org" ] || [ "$APP_SCOPE" = "user" ] || fail "APP_SCOPE must be 'org' or 'user' (got '$APP_SCOPE')"
echo "App ownership scope: $APP_SCOPE"
echo ""

# Base URL for creating a new GitHub App, depending on scope.
app_create_base_url() {
  if [ "$APP_SCOPE" = "org" ]; then
    echo "https://github.com/organizations/${GITHUB_ORG}/settings/apps/new"
  else
    echo "https://github.com/settings/apps/new"   # creates under the authenticated user
  fi
}

# Base URL for registering a new OAuth App, depending on scope.
oauth_create_base_url() {
  if [ "$APP_SCOPE" = "org" ]; then
    echo "https://github.com/organizations/${GITHUB_ORG}/settings/applications/new"
  else
    echo "https://github.com/settings/applications/new"
  fi
}

# Look up an app's installation id. Org scope reads the org's installations;
# user scope reads the authenticated user's installations (works for apps
# installed on personal repos OR on org repos the user can access).
lookup_install_id() {
  local app_id="$1"
  if [ "$APP_SCOPE" = "org" ]; then
    gh api "/orgs/$GITHUB_ORG/installations" --jq ".installations[] | select(.app_id==$app_id) | .id" 2>/dev/null || echo ""
  else
    gh api "/user/installations" --jq ".installations[] | select(.app_id==$app_id) | .id" 2>/dev/null || echo ""
  fi
}

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
  EXISTING_ID=$(aws secretsmanager get-secret-value --secret-id "adp/${GITHUB_ORG}/gh-app-${ROLE}-id" --query 'SecretString' --output text --region "$AWS_REGION" 2>/dev/null || echo "")
  EXISTING_KEY=$(aws secretsmanager get-secret-value --secret-id "adp/${GITHUB_ORG}/gh-app-${ROLE}-key" --query 'SecretString' --output text --region "$AWS_REGION" 2>/dev/null || echo "")

  if [ -n "$EXISTING_ID" ] && [ -n "$EXISTING_KEY" ] && [ "$EXISTING_KEY" != "" ]; then
    ok "App $APP_NAME already configured (ID: $EXISTING_ID)"
    APP_ID="$EXISTING_ID"

    # Check if installed — if not, open browser for installation
    INSTALL_ID=$(lookup_install_id "$APP_ID")
    if [ -n "$INSTALL_ID" ]; then
      ok "Already installed (installation ID: $INSTALL_ID)"
    else
      echo "  App exists but is NOT installed yet. Opening browser..."
      INSTALL_URL="https://github.com/apps/${APP_NAME}/installations/select_target"
      open_url "$INSTALL_URL"
      echo -n "  Press Enter after you've installed the app in the browser..."
      read -r
      sleep 2
      INSTALL_ID=$(lookup_install_id "$APP_ID")
      [ -n "$INSTALL_ID" ] && ok "Installed (ID: $INSTALL_ID)" || warn "Could not verify installation"
    fi
    continue
  fi

  # Build the manifest URL with pre-filled permissions.
  # Base differs by scope: org-owned vs user-owned (see app_create_base_url).
  URL="$(app_create_base_url)"
  URL="${URL}?name=${APP_NAME}"
  URL="${URL}&url=https://github.com/${GITHUB_ORG}/adp"
  URL="${URL}&public=false"
  URL="${URL}&webhook_active=false"
  # Repository-level permissions — apply to both org- and user-owned apps.
  URL="${URL}&contents=write"
  URL="${URL}&issues=write"
  URL="${URL}&pull_requests=write"
  URL="${URL}&checks=write"
  URL="${URL}&workflows=write"
  URL="${URL}&metadata=read"
  # administration:write lets ARC register repo-scoped self-hosted runners.
  # actions:write lets those runners claim and run workflow jobs.
  URL="${URL}&administration=write"
  URL="${URL}&actions=write"
  # Organization-level permissions — only meaningful for org-owned apps. A
  # user-owned app (repo-admin path) can't grant org perms, and requesting them
  # just clutters the consent screen, so omit them in user scope. The agent
  # features that use members:read / organization_projects (e.g. PM project
  # boards) are org-only and not available in the personal-repo deployment.
  if [ "$APP_SCOPE" = "org" ]; then
    URL="${URL}&members=read"
    URL="${URL}&organization_projects=write"
  fi
  URL="${URL}&events[]=issues"
  URL="${URL}&events[]=issue_comment"
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
  store_secret "adp/${GITHUB_ORG}/gh-app-${ROLE}-id" "$APP_ID"
  store_secret "adp/${GITHUB_ORG}/gh-app-${ROLE}-key" "$(cat "$PEM_FILE")"
  ok "Stored adp/${GITHUB_ORG}/gh-app-${ROLE}-id and adp/${GITHUB_ORG}/gh-app-${ROLE}-key"

  # Install the app on the org
  echo ""
  echo "  Now install the app."
  if [ "$APP_SCOPE" = "org" ]; then
    echo "  Opening browser — select the '$GITHUB_ORG' org, choose 'Only select repositories', pick 'adp'."
  else
    echo "  Opening browser — select your user account, choose 'Only select repositories',"
    echo "  and pick the repo(s) you administer (e.g. '$GITHUB_ORG/adp' if it's under your account,"
    echo "  or whichever repo you onboarded). Repo-admin access is sufficient — no org rights needed."
  fi
  echo ""

  INSTALL_URL="https://github.com/apps/${APP_NAME}/installations/select_target"
  open_url "$INSTALL_URL"

  echo -n "  Press Enter after you've installed the app in the browser..."
  read -r

  # Verify installation and add extra repos
  echo "  Verifying installation..."
  sleep 2
  INSTALL_ID=$(lookup_install_id "$APP_ID")

  if [ -n "$INSTALL_ID" ]; then
    ok "App installed (installation ID: $INSTALL_ID)"

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

# =============================================================================
# Step 4 — GitHub OAuth App (for SPA login via the auth broker)
# =============================================================================
# A GitHub OAuth App is a *separate* primitive from a GitHub App:
#   - GitHub App     → server-to-server (agents reading/writing repos)
#   - OAuth App      → user-driven login flow on the SPA
#
# The auth broker Lambda (modules/gateway/lambda/github-auth-broker) reads
# the OAuth client_id + client_secret from:
#   adp/${ADP_ENV}/cognito/github-oauth-credentials
#
# Without this, login to the ADP UI fails with "client_id parameter is invalid"
# because gateway-infra-apply.yml seeds the secret with PLACEHOLDER values.

echo ""
echo -e "${BLUE}━━━ App 4/4: ${GITHUB_ORG}-adp-oauth (GitHub OAuth App for SPA login) ━━━${NC}"
echo ""

# Check if already configured (non-placeholder values).
OAUTH_SECRET_ID="adp/${ADP_ENV}/cognito/github-oauth-credentials"
EXISTING_OAUTH=$(aws secretsmanager get-secret-value --secret-id "$OAUTH_SECRET_ID" --query 'SecretString' --output text --region "$AWS_REGION" 2>/dev/null || echo "")
EXISTING_CLIENT_ID=$(echo "$EXISTING_OAUTH" | python3 -c 'import sys, json; d=json.load(sys.stdin); print(d.get("client_id",""))' 2>/dev/null || echo "")

if [ -n "$EXISTING_CLIENT_ID" ] && [ "$EXISTING_CLIENT_ID" != "PLACEHOLDER" ]; then
  ok "OAuth App already configured (client_id=${EXISTING_CLIENT_ID})"
else
  # Resolve the API Gateway invoke URL from SSM so we know the callback URL.
  APIGW_URL=$(aws ssm get-parameter --name "/adp/${ADP_ENV}/gateway/apigw-invoke-url" --query Parameter.Value --output text --region "$AWS_REGION" 2>/dev/null || echo "")

  if [ -z "$APIGW_URL" ]; then
    warn "SSM /adp/${ADP_ENV}/gateway/apigw-invoke-url is empty."
    warn "Run gateway-infra-apply.yml against this account first, then re-run this script."
    warn "Skipping OAuth App creation for now."
  else
    CALLBACK_URL="${APIGW_URL}/auth/github/callback"
    HOMEPAGE_URL=$(aws ssm get-parameter --name "/adp/${ADP_ENV}/gateway/cloudfront-domain" --query Parameter.Value --output text --region "$AWS_REGION" 2>/dev/null | sed 's|^|https://|' || echo "")
    [ -z "$HOMEPAGE_URL" ] || [ "$HOMEPAGE_URL" = "https://" ] && HOMEPAGE_URL="$CALLBACK_URL"

    # Pre-fill the OAuth App registration form with name + URLs.
    OAUTH_APP_NAME="${GITHUB_ORG}-adp-oauth"
    OAUTH_URL="$(oauth_create_base_url)"
    OAUTH_URL="${OAUTH_URL}?oauth_application[name]=${OAUTH_APP_NAME}"
    OAUTH_URL="${OAUTH_URL}&oauth_application[url]=${HOMEPAGE_URL}"
    OAUTH_URL="${OAUTH_URL}&oauth_application[callback_url]=${CALLBACK_URL}"

    echo "  Opening browser to register $OAUTH_APP_NAME..."
    echo ""
    echo "  In the browser:"
    echo "    1. Click 'Register application'"
    echo "    2. On the next page, copy the 'Client ID' shown"
    echo "    3. Click 'Generate a new client secret', copy the value (only shown once)"
    echo ""
    echo "  Pre-filled values:"
    echo "    Application name:           $OAUTH_APP_NAME"
    echo "    Homepage URL:               $HOMEPAGE_URL"
    echo "    Authorization callback URL: $CALLBACK_URL"
    echo ""

    open_url "$OAUTH_URL"

    echo -n "  Enter the OAuth Client ID: "
    read -r OAUTH_CLIENT_ID
    [ -z "$OAUTH_CLIENT_ID" ] && fail "No Client ID provided"

    # Read secret without echoing
    echo -n "  Enter the OAuth Client Secret (input hidden): "
    stty -echo
    read -r OAUTH_CLIENT_SECRET
    stty echo
    echo ""
    [ -z "$OAUTH_CLIENT_SECRET" ] && fail "No Client Secret provided"

    # Store as JSON, format the broker reads.
    OAUTH_JSON=$(python3 -c 'import json,sys; print(json.dumps({"client_id":sys.argv[1],"client_secret":sys.argv[2]}))' "$OAUTH_CLIENT_ID" "$OAUTH_CLIENT_SECRET")
    store_secret "$OAUTH_SECRET_ID" "$OAUTH_JSON"
    ok "Stored OAuth credentials in $OAUTH_SECRET_ID"

    # Force the broker Lambda to drop its cached secret on next cold start.
    # The handler caches _github_client_secret module-level. Bumping an env
    # var triggers a new execution context.
    BROKER_FN="bedrockgw-${ADP_ENV}-github-auth-broker"
    if aws lambda get-function --function-name "$BROKER_FN" --region "$AWS_REGION" >/dev/null 2>&1; then
      CURRENT_ENV=$(aws lambda get-function-configuration --function-name "$BROKER_FN" --region "$AWS_REGION" --query 'Environment.Variables' --output json 2>/dev/null || echo "{}")
      NEW_ENV=$(echo "$CURRENT_ENV" | python3 -c 'import json,sys,time; e=json.load(sys.stdin) or {}; e["OAUTH_SECRET_REFRESH_AT"]=str(int(time.time())); print(json.dumps({"Variables":e}))')
      aws lambda update-function-configuration \
        --function-name "$BROKER_FN" \
        --environment "$NEW_ENV" \
        --region "$AWS_REGION" >/dev/null 2>&1 \
        && ok "Forced broker Lambda cold-start so it picks up the new secret" \
        || warn "Could not bump broker env (next natural cold start within ~15 min will pick up the secret)"
    else
      warn "Broker Lambda $BROKER_FN not deployed yet; the secret will be picked up on first cold start"
    fi
  fi
fi

# Summary
echo ""
echo "========================================="
echo -e "${BLUE}Final Validation${NC}"
echo "========================================="
echo ""

ALL_GOOD=true

echo "Secrets:"
for ROLE in dev pm ops; do
  ID=$(aws secretsmanager get-secret-value --secret-id "adp/${GITHUB_ORG}/gh-app-${ROLE}-id" --query 'SecretString' --output text --region "$AWS_REGION" 2>/dev/null || echo "")
  KEY_LEN=$(aws secretsmanager get-secret-value --secret-id "adp/${GITHUB_ORG}/gh-app-${ROLE}-key" --query 'SecretString' --output text --region "$AWS_REGION" 2>/dev/null | wc -c | tr -d ' ')
  if [ -n "$ID" ] && [ "$KEY_LEN" -gt 100 ] 2>/dev/null; then
    ok "adp/${GITHUB_ORG}/gh-app-${ROLE}: ID=$ID, key=${KEY_LEN} chars"
  else
    fail "adp/${GITHUB_ORG}/gh-app-${ROLE}: MISSING or invalid"
    ALL_GOOD=false
  fi
done

echo ""
echo "Installations:"
for i in 0 1 2; do
  ROLE="${APPS[$i]}"
  APP_NAME="${APP_NAMES[$i]}"
  APP_ID=$(aws secretsmanager get-secret-value --secret-id "adp/${GITHUB_ORG}/gh-app-${ROLE}-id" --query 'SecretString' --output text --region "$AWS_REGION" 2>/dev/null || echo "0")
  INSTALL_ID=$(lookup_install_id "$APP_ID")
  if [ -n "$INSTALL_ID" ]; then
    REPOS=$(gh api "/user/installations/$INSTALL_ID/repositories" --jq '[.repositories[].name] | join(", ")' 2>/dev/null || echo "?")
    ok "$APP_NAME installed (ID: $INSTALL_ID) → repos: $REPOS"
  else
    fail "$APP_NAME NOT installed on $GITHUB_ORG"
    ALL_GOOD=false
  fi
done

echo ""
echo "OAuth App:"
OAUTH_CHECK=$(aws secretsmanager get-secret-value --secret-id "adp/${ADP_ENV}/cognito/github-oauth-credentials" --query 'SecretString' --output text --region "$AWS_REGION" 2>/dev/null || echo "")
OAUTH_CID=$(echo "$OAUTH_CHECK" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("client_id",""))' 2>/dev/null || echo "")
if [ -n "$OAUTH_CID" ] && [ "$OAUTH_CID" != "PLACEHOLDER" ]; then
  ok "adp/${ADP_ENV}/cognito/github-oauth-credentials: client_id=$OAUTH_CID"
else
  warn "adp/${ADP_ENV}/cognito/github-oauth-credentials: PLACEHOLDER or missing — login will not work"
  ALL_GOOD=false
fi

echo ""
if [ "$ALL_GOOD" = true ]; then
  echo -e "${GREEN}=========================================${NC}"
  echo -e "${GREEN}All GitHub Apps + OAuth App configured${NC}"
  echo -e "${GREEN}=========================================${NC}"
  echo ""
  echo "Next: ./platform/scripts/deploy-all.sh"
else
  echo -e "${RED}=========================================${NC}"
  echo -e "${RED}Some apps are missing or not configured${NC}"
  echo -e "${RED}=========================================${NC}"
  echo ""
  echo "Re-run this script to fix: $0 $GITHUB_ORG"
fi
