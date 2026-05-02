#!/bin/bash
set -euo pipefail

# =============================================================================
# ADP — Register Public GitHub App: ADP Agent Platform
# =============================================================================
# Creates (or verifies) the hosted public GitHub App that customers install.
# Stores the App ID and private key in Secrets Manager.
#
# Usage:
#   ./register-github-app.sh <github-org> [--env dev] [--webhook-url URL]
#
# Prerequisites:
#   - AWS CLI configured with appropriate permissions
#   - gh CLI authenticated
#   - Secrets Manager secret adp/<env>/webhook-ingress/github-webhook-secret exists
# =============================================================================

GITHUB_ORG="${1:-}"
shift 2>/dev/null || true

ENVIRONMENT="dev"
WEBHOOK_URL=""
AWS_REGION="${AWS_REGION:-us-east-1}"
APP_NAME_BASE="adp-agent-platform"

# Parse optional flags
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)
      ENVIRONMENT="$2"
      shift 2
      ;;
    --webhook-url)
      WEBHOOK_URL="$2"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}⚠${NC} $1"; }
fail() { echo -e "${RED}✗${NC} $1"; exit 1; }
info() { echo -e "${BLUE}ℹ${NC} $1"; }

if [ -z "$GITHUB_ORG" ]; then
  echo "Usage: $0 <github-org> [--env dev] [--webhook-url URL]"
  echo ""
  echo "Registers the public 'ADP Agent Platform' GitHub App."
  echo ""
  echo "Options:"
  echo "  --env ENV            Environment (default: dev)"
  echo "  --webhook-url URL    Override webhook URL (default: auto-detect from Terraform output)"
  echo ""
  echo "The script will:"
  echo "  1. Open your browser to create the GitHub App with pre-filled settings"
  echo "  2. Store the App ID in Secrets Manager"
  echo "  3. Store the private key in Secrets Manager"
  echo "  4. Configure the webhook URL and secret"
  exit 1
fi

# Validate prerequisites
command -v aws &>/dev/null || fail "AWS CLI not installed"
command -v gh &>/dev/null  || fail "GitHub CLI (gh) not installed"

# Secrets Manager paths
SECRET_ID_PATH="adp/${ENVIRONMENT}/github-app/adp-agent-platform-id"
SECRET_KEY_PATH="adp/${ENVIRONMENT}/github-app/adp-agent-platform-key"
WEBHOOK_SECRET_PATH="adp/${ENVIRONMENT}/webhook-ingress/github-webhook-secret"

# Detect Downloads folder
if [ -d "$HOME/Downloads" ]; then
  DOWNLOADS="$HOME/Downloads"
elif [ -d "$HOME/download" ]; then
  DOWNLOADS="$HOME/download"
else
  DOWNLOADS="/tmp"
fi

# =============================================================================
# Helper functions
# =============================================================================

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

find_latest_pem() {
  ls -t "$DOWNLOADS"/*.pem 2>/dev/null | head -1
}

store_secret() {
  local name="$1"
  local value="$2"
  if aws secretsmanager describe-secret --secret-id "$name" --region "$AWS_REGION" &>/dev/null; then
    aws secretsmanager put-secret-value \
      --secret-id "$name" \
      --secret-string "$value" \
      --region "$AWS_REGION" > /dev/null
    ok "Updated secret: $name"
  else
    aws secretsmanager create-secret \
      --name "$name" \
      --secret-string "$value" \
      --region "$AWS_REGION" > /dev/null
    ok "Created secret: $name"
  fi
}

# =============================================================================
# Step 1: Resolve webhook URL
# =============================================================================

echo ""
echo "ADP Agent Platform — GitHub App Registration"
echo "============================================="
echo "Org: $GITHUB_ORG | Env: $ENVIRONMENT"
echo ""

if [ -z "$WEBHOOK_URL" ]; then
  info "Detecting webhook URL from Terraform state..."
  WEBHOOK_URL=$(cd "$(dirname "$0")/../infra" && terraform output -raw webhook_url 2>/dev/null || echo "")
  if [ -z "$WEBHOOK_URL" ]; then
    # Fallback: try SSM parameter
    WEBHOOK_URL=$(aws ssm get-parameter \
      --name "/adp/${ENVIRONMENT}/webhook-ingress/webhook-url" \
      --query "Parameter.Value" --output text \
      --region "$AWS_REGION" 2>/dev/null || echo "")
  fi
  if [ -z "$WEBHOOK_URL" ]; then
    fail "Cannot auto-detect webhook URL. Pass --webhook-url explicitly."
  fi
fi

ok "Webhook URL: $WEBHOOK_URL"

# =============================================================================
# Step 2: Retrieve webhook secret
# =============================================================================

info "Retrieving webhook secret from Secrets Manager..."
WEBHOOK_SECRET=$(aws secretsmanager get-secret-value \
  --secret-id "$WEBHOOK_SECRET_PATH" \
  --query 'SecretString' --output text \
  --region "$AWS_REGION" 2>/dev/null || echo "")

if [ -z "$WEBHOOK_SECRET" ] || [ "$WEBHOOK_SECRET" = "PLACEHOLDER_REPLACE_WITH_ACTUAL_SECRET" ]; then
  warn "Webhook secret is a placeholder or missing. Generating a new one..."
  WEBHOOK_SECRET=$(openssl rand -hex 32)
  store_secret "$WEBHOOK_SECRET_PATH" "$WEBHOOK_SECRET"
  ok "Generated and stored new webhook secret"
fi

ok "Webhook secret retrieved"

# =============================================================================
# Step 3: Check if app already exists
# =============================================================================

EXISTING_ID=$(aws secretsmanager get-secret-value \
  --secret-id "$SECRET_ID_PATH" \
  --query 'SecretString' --output text \
  --region "$AWS_REGION" 2>/dev/null || echo "")

EXISTING_KEY=$(aws secretsmanager get-secret-value \
  --secret-id "$SECRET_KEY_PATH" \
  --query 'SecretString' --output text \
  --region "$AWS_REGION" 2>/dev/null || echo "")

if [ -n "$EXISTING_ID" ] && [ -n "$EXISTING_KEY" ] && [ ${#EXISTING_KEY} -gt 100 ]; then
  ok "App already registered (ID: $EXISTING_ID)"
  echo ""
  info "To re-register, delete the secrets first:"
  info "  aws secretsmanager delete-secret --secret-id $SECRET_ID_PATH --force-delete-without-recovery"
  info "  aws secretsmanager delete-secret --secret-id $SECRET_KEY_PATH --force-delete-without-recovery"
  echo ""

  # Verify webhook is configured correctly on the existing app
  info "Verifying webhook configuration..."
  APP_WEBHOOK=$(gh api "/app" --jq '.events // [] | join(",")' 2>/dev/null || echo "")
  if [ -n "$APP_WEBHOOK" ]; then
    ok "App webhook events: $APP_WEBHOOK"
  fi
  exit 0
fi

# =============================================================================
# Step 4: Create the GitHub App via browser
# =============================================================================

# GitHub App names are globally unique. Try the base name first, fall back to org-prefixed.
APP_NAME="${APP_NAME_BASE}"

echo ""
echo -e "${BLUE}━━━ Creating GitHub App: ${APP_NAME} ━━━${NC}"
echo ""
echo "  Permissions:"
echo "    - contents: write    (clone repos, push branches)"
echo "    - issues: write      (read issues, post comments, manage labels)"
echo "    - pull_requests: write (open and update PRs)"
echo "    - checks: write      (create check runs for progress UX)"
echo "    - metadata: read     (list repos the app is installed on)"
echo ""
echo "  Subscribed events:"
echo "    issues, issue_comment, pull_request, pull_request_review,"
echo "    pull_request_review_comment, label"
echo ""
echo "  Webhook URL: $WEBHOOK_URL"
echo ""

# Build the manifest URL with pre-filled settings
# Note: public=true because this is the hosted platform app customers install
URL="https://github.com/organizations/${GITHUB_ORG}/settings/apps/new"
URL="${URL}?name=${APP_NAME}"
URL="${URL}&url=https://github.com/${GITHUB_ORG}/adp"
URL="${URL}&public=true"
URL="${URL}&webhook_active=true"
URL="${URL}&webhook_url=${WEBHOOK_URL}"
URL="${URL}&contents=write"
URL="${URL}&issues=write"
URL="${URL}&pull_requests=write"
URL="${URL}&checks=write"
URL="${URL}&metadata=read"
URL="${URL}&events[]=issues"
URL="${URL}&events[]=issue_comment"
URL="${URL}&events[]=pull_request"
URL="${URL}&events[]=pull_request_review"
URL="${URL}&events[]=pull_request_review_comment"
URL="${URL}&events[]=label"

echo "  Opening browser to create the app..."
echo ""
echo "  In the browser:"
echo "    1. IMPORTANT: Set the Webhook secret field to the value below:"
echo ""
echo "       $WEBHOOK_SECRET"
echo ""
echo "    2. Review the pre-filled permissions and click 'Create GitHub App'"
echo "    3. Note the App ID shown at the top of the next page"
echo "    4. Scroll down → click 'Generate a private key' (downloads a .pem file)"
echo ""

open_url "$URL"

# Wait for user to enter the App ID
echo -n "  Enter the App ID: "
read -r APP_ID

if [ -z "$APP_ID" ]; then
  fail "No App ID provided"
fi

# Validate it's a number
if ! [[ "$APP_ID" =~ ^[0-9]+$ ]]; then
  fail "App ID must be a number, got: $APP_ID"
fi

# =============================================================================
# Step 5: Locate and store the private key
# =============================================================================

echo "  Looking for the private key .pem file..."
sleep 2

PEM_FILE=""
PEM_FILE=$(find_latest_pem)

if [ -z "$PEM_FILE" ]; then
  echo ""
  echo "  Could not auto-detect the .pem file in $DOWNLOADS."
  echo -n "  Enter the path to the downloaded .pem file: "
  read -r PEM_FILE
fi

if [ ! -f "$PEM_FILE" ]; then
  fail "Private key file not found: $PEM_FILE"
fi

# Validate it looks like a PEM
if ! grep -q "BEGIN.*PRIVATE KEY" "$PEM_FILE" 2>/dev/null; then
  fail "File does not appear to be a valid PEM private key: $PEM_FILE"
fi

ok "Found private key: $PEM_FILE"

# =============================================================================
# Step 6: Store credentials in Secrets Manager
# =============================================================================

echo ""
info "Storing credentials in Secrets Manager..."

store_secret "$SECRET_ID_PATH" "$APP_ID"
store_secret "$SECRET_KEY_PATH" "$(cat "$PEM_FILE")"

# =============================================================================
# Step 7: Verify
# =============================================================================

echo ""
echo -e "${BLUE}━━━ Verification ━━━${NC}"
echo ""

STORED_ID=$(aws secretsmanager get-secret-value \
  --secret-id "$SECRET_ID_PATH" \
  --query 'SecretString' --output text \
  --region "$AWS_REGION" 2>/dev/null || echo "")

STORED_KEY_LEN=$(aws secretsmanager get-secret-value \
  --secret-id "$SECRET_KEY_PATH" \
  --query 'SecretString' --output text \
  --region "$AWS_REGION" 2>/dev/null | wc -c | tr -d ' ')

if [ "$STORED_ID" = "$APP_ID" ]; then
  ok "App ID stored correctly: $STORED_ID"
else
  fail "App ID mismatch in Secrets Manager"
fi

if [ "$STORED_KEY_LEN" -gt 100 ] 2>/dev/null; then
  ok "Private key stored (${STORED_KEY_LEN} bytes)"
else
  fail "Private key appears too short or missing"
fi

echo ""
echo -e "${GREEN}=========================================${NC}"
echo -e "${GREEN}GitHub App registered successfully!${NC}"
echo -e "${GREEN}=========================================${NC}"
echo ""
echo "  App Name:    $APP_NAME"
echo "  App ID:      $APP_ID"
echo "  Webhook URL: $WEBHOOK_URL"
echo "  Secrets:"
echo "    ID:  $SECRET_ID_PATH"
echo "    Key: $SECRET_KEY_PATH"
echo ""
echo "Next steps:"
echo "  1. Install the app on a test repo"
echo "  2. Label an issue to trigger a webhook"
echo "  3. Check the webhook-events DynamoDB table for the event"
echo ""
