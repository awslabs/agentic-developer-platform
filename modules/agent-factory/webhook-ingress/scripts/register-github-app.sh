#!/bin/bash
set -euo pipefail

# =============================================================================
# ADP — Register GitHub App (ADP Agent Platform) + wire it into the platform
# =============================================================================
# CLI FALLBACK for GitHub App registration. The PRIMARY path is the UI:
#   Settings → Connections → "Set up GitHub App" (manifest flow)
# performed by the Phase-6d bootstrap platform_admin. Use this script for
# headless / CI environments where no browser session is available.
#
# Creates (or verifies) the GitHub App customers install, stores its App ID +
# private key in Secrets Manager, then calls wire-github-app.sh to point the
# running platform (gateway UI install flow + GitHub login) at it.
#
# Designed to be runnable NON-INTERACTIVELY: pass --app-id / --pem-path /
# --client-secret / --visibility as flags and it won't prompt. Any input not
# supplied as a flag is prompted for interactively (the browser-create flow).
#
# Usage:
#   ./register-github-app.sh <github-org> [options]
#
# Options (all optional except <github-org>):
#   --env ENV              Environment (default: dev)                  [ADP_ENV]
#   --webhook-url URL      Webhook URL (default: auto-detect from TF output)
#   --visibility V         private (default) | public                  [APP_VISIBILITY]
#   --app-id ID            App ID — skips the "Enter the App ID" prompt
#   --pem-path PATH        Private key .pem — skips auto-detect/prompt
#   --client-secret SECRET OAuth client secret (for GitHub login wiring). Shown
#                          once at app creation; pass it to wire login in the
#                          same run.                                    [GH_APP_CLIENT_SECRET]
#   --region REGION        AWS region (default: us-east-1)              [AWS_REGION]
#   --no-wire              Register only; skip calling wire-github-app.sh.
#
# Prerequisites: aws CLI + creds; gh authenticated; the webhook-ingress stack
# applied (provides the webhook URL + secret).
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GITHUB_ORG="${1:-}"
shift 2>/dev/null || true

ENVIRONMENT="${ADP_ENV:-dev}"
WEBHOOK_URL=""
AWS_REGION="${AWS_REGION:-us-east-1}"
APP_NAME_BASE="adp-agent-platform"
APP_VISIBILITY="${APP_VISIBILITY:-}"
APP_ID_FLAG=""
PEM_PATH_FLAG=""
CLIENT_SECRET="${GH_APP_CLIENT_SECRET:-}"
NO_WIRE=false
# Owner of the GitHub App. "org" creates it under the org (requires org-owner
# rights); "user" creates it under the caller's personal account (any user, no
# org-admin needed) — the escape hatch for operators without org-admin.
OWNER_TYPE="${APP_OWNER_TYPE:-org}"
# Optional install-target repo (owner/name); only used to print the exact
# install URL at the end. Defaults to "<github-org>/adp".
INSTALL_REPO="${APP_INSTALL_REPO:-}"

# Parse optional flags
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)            ENVIRONMENT="$2"; shift 2 ;;
    --webhook-url)    WEBHOOK_URL="$2"; shift 2 ;;
    --visibility)     APP_VISIBILITY="$2"; shift 2 ;;
    --owner-type)     OWNER_TYPE="$2"; shift 2 ;;
    --repo)           INSTALL_REPO="$2"; shift 2 ;;
    --app-id)         APP_ID_FLAG="$2"; shift 2 ;;
    --pem-path)       PEM_PATH_FLAG="$2"; shift 2 ;;
    --client-secret)  CLIENT_SECRET="$2"; shift 2 ;;
    --region)         AWS_REGION="$2"; shift 2 ;;
    --no-wire)        NO_WIRE=true; shift ;;
    *)                shift ;;
  esac
done

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}⚠${NC} $1"; }
fail() { echo -e "${RED}✗${NC} $1"; exit 1; }
info() { echo -e "${BLUE}ℹ${NC} $1"; }

if [ -z "$GITHUB_ORG" ]; then
  echo "Usage: $0 <github-org> [options]"
  echo ""
  echo "Registers the 'adp-agent-platform' GitHub App and wires it into the platform."
  echo ""
  echo "Options:"
  echo "  --env ENV              Environment (default: dev)"
  echo "  --webhook-url URL      Override webhook URL (default: auto-detect from TF output)"
  echo "  --visibility V         private (default) | public  (also via APP_VISIBILITY env)"
  echo "  --owner-type T         org (default) | user. 'org' creates the App under the"
  echo "                         org (needs org-OWNER rights); 'user' creates it under"
  echo "                         your personal account (no org-admin needed). [APP_OWNER_TYPE]"
  echo "  --repo OWNER/NAME      Install-target repo, for the printed install URL"
  echo "                         (default: <github-org>/adp).                  [APP_INSTALL_REPO]"
  echo "  --app-id ID            App ID — skips the interactive 'Enter the App ID' prompt"
  echo "  --pem-path PATH        Private key .pem path — skips ~/Downloads auto-detect"
  echo "  --client-secret SECRET OAuth client secret → wires GitHub LOGIN in the same run"
  echo "  --region REGION        AWS region (default: us-east-1)"
  echo "  --no-wire              Register only; don't call wire-github-app.sh"
  echo ""
  echo "Don't have org-owner rights on '<github-org>'? Use --owner-type user to create a"
  echo "  personal App under your own account instead (any user can; install it on repos"
  echo "  you admin). For a shared team deployment, have an org owner run it with the"
  echo "  default --owner-type org."
  echo ""
  echo "Interactive by default (opens browser, prompts for App ID + .pem). Supply"
  echo "  --app-id/--pem-path/--visibility to run non-interactively. After storing the"
  echo "  app creds it calls wire-github-app.sh to point the gateway UI install flow"
  echo "  (and, with --client-secret, GitHub login) at the new app."
  exit 1
fi

case "$OWNER_TYPE" in
  org|user) ;;
  *) fail "--owner-type must be 'org' or 'user' (got '$OWNER_TYPE')" ;;
esac
# Default the install repo to <github-org>/adp when not supplied.
INSTALL_REPO="${INSTALL_REPO:-${GITHUB_ORG}/adp}"

# Validate prerequisites
command -v aws &>/dev/null || fail "AWS CLI not installed"
command -v gh &>/dev/null  || fail "GitHub CLI (gh) not installed"

# -----------------------------------------------------------------------------
# App visibility — private (recommended) vs public
# -----------------------------------------------------------------------------
# A GitHub App's visibility controls WHO can install it:
#   - private: only the owning org/user can install it. The UI "Link GitHub"
#              flow still works for users INSIDE the owning org (subject to the
#              org's "allow members to install GitHub Apps" setting).
#   - public:  ANY org/user can install it. Required only when ADP will be used
#              by tenants in OTHER GitHub orgs you don't own (true cross-org
#              multi-tenant / hosted SaaS).
#
# Recommendation: PRIVATE. Choose public ONLY for cross-org multi-tenant.
# Set APP_VISIBILITY=private|public to skip the prompt (useful for automation).
APP_VISIBILITY="${APP_VISIBILITY:-}"
if [ -z "$APP_VISIBILITY" ]; then
  echo ""
  echo "Should the GitHub App be private or public?"
  echo "  1) Private (recommended) — only the '$GITHUB_ORG' org can install it."
  echo "                             Fine for single-org use, even with many"
  echo "                             teams/repos onboarding via the UI."
  echo "  2) Public                — ANY GitHub org can install it. Choose this"
  echo "                             ONLY if external orgs (tenants you don't own)"
  echo "                             will use the platform."
  echo -n "Choose [1/2] (default 1 = private): "
  read -r _vis_choice
  case "${_vis_choice:-1}" in
    2) APP_VISIBILITY="public" ;;
    *) APP_VISIBILITY="private" ;;
  esac
fi
[ "$APP_VISIBILITY" = "private" ] || [ "$APP_VISIBILITY" = "public" ] \
  || fail "APP_VISIBILITY must be 'private' or 'public' (got '$APP_VISIBILITY')"
# Manifest form field is public=true|false.
APP_PUBLIC=$([ "$APP_VISIBILITY" = "public" ] && echo "true" || echo "false")
echo "App visibility: $APP_VISIBILITY"
if [ "$APP_VISIBILITY" = "public" ]; then
  warn "Public app — installable by ANY GitHub org. Only correct for cross-org multi-tenant."
fi

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
echo "Org: $GITHUB_ORG | Owner: $OWNER_TYPE | Install repo: $INSTALL_REPO | Env: $ENVIRONMENT"
[ "$OWNER_TYPE" = "org" ] && info "Creating an ORG-owned App — needs org-OWNER rights on '$GITHUB_ORG'. No org-admin? re-run with --owner-type user."
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

# Build the manifest URL with pre-filled settings.
# Visibility (public=true|false) is operator-chosen above: private is the
# recommended default; public only for cross-org multi-tenant.
# Owner-scoped create-App page: org page needs org-owner rights; the user page
# (github.com/settings/apps/new) works for any account without org-admin.
if [ "$OWNER_TYPE" = "user" ]; then
  URL="https://github.com/settings/apps/new"
else
  URL="https://github.com/organizations/${GITHUB_ORG}/settings/apps/new"
fi
URL="${URL}?name=${APP_NAME}"
URL="${URL}&url=https://github.com/${INSTALL_REPO}"
URL="${URL}&public=${APP_PUBLIC}"
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

# Only open the browser + prompt when the App ID wasn't supplied as a flag.
if [ -n "$APP_ID_FLAG" ]; then
  APP_ID="$APP_ID_FLAG"
  info "Using App ID from --app-id: $APP_ID (skipping browser create prompt)"
else
  open_url "$URL"
  # Wait for user to enter the App ID
  echo -n "  Enter the App ID: "
  read -r APP_ID
fi

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

PEM_FILE=""
if [ -n "$PEM_PATH_FLAG" ]; then
  PEM_FILE="$PEM_PATH_FLAG"
  info "Using private key from --pem-path: $PEM_FILE"
else
  echo "  Looking for the private key .pem file..."
  sleep 2
  PEM_FILE=$(find_latest_pem)
  if [ -z "$PEM_FILE" ]; then
    echo ""
    echo "  Could not auto-detect the .pem file in $DOWNLOADS."
    echo -n "  Enter the path to the downloaded .pem file: "
    read -r PEM_FILE
  fi
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

# =============================================================================
# Step 8: Wire the app into the running platform (gateway UI install + login)
# =============================================================================
# Registering only stores the app creds. wire-github-app.sh points the running
# services at this app: the gateway's "Link GitHub" install flow
# (BG_GITHUB_APP_SLUG/ID/PRIVATE_KEY) and — if --client-secret was provided —
# GitHub login (broker OAuth client_id/secret). Kept as a separate, independently
# re-runnable script (e.g. to re-wire after a gateway redeploy or secret rotation).
WIRE_SCRIPT="${SCRIPT_DIR}/wire-github-app.sh"
if [ "$NO_WIRE" = true ]; then
  info "--no-wire set; skipping platform wiring."
  info "Run it later:  ${WIRE_SCRIPT} --app-slug ${APP_NAME} --env ${ENVIRONMENT} [--client-secret <secret>]"
elif [ ! -x "$WIRE_SCRIPT" ]; then
  warn "wire-github-app.sh not found/executable at $WIRE_SCRIPT — skipping wiring."
  warn "Run it manually:  bash ${WIRE_SCRIPT} --app-slug ${APP_NAME} --env ${ENVIRONMENT}"
else
  echo -e "${BLUE}━━━ Wiring the app into the running platform ━━━${NC}"
  WIRE_ARGS=(--app-slug "$APP_NAME" --env "$ENVIRONMENT" --region "$AWS_REGION")
  [ -n "$CLIENT_SECRET" ] && WIRE_ARGS+=(--client-secret "$CLIENT_SECRET")
  # AWS_REGION already in env; pass through. The wire script auto-fetches the
  # OAuth client_id via `gh api /apps/<slug>`.
  AWS_REGION="$AWS_REGION" bash "$WIRE_SCRIPT" "${WIRE_ARGS[@]}" || \
    warn "Wiring step reported an issue — review output above; you can re-run wire-github-app.sh."
fi

echo ""
echo "Next steps:"
echo "  1. Install the app (suggested target repo: ${INSTALL_REPO}):"
echo "       https://github.com/apps/${APP_NAME}/installations/new"
echo "  2. @mention an agent in an issue/PR comment (e.g. @agent-developer ...)"
echo "  3. Check the webhook-events DynamoDB table / kubectl get pods -n adp-agents"
if [ -z "$CLIENT_SECRET" ] && [ "$NO_WIRE" = false ]; then
  echo ""
  warn "GitHub LOGIN not wired (no --client-secret). To enable UI login: generate an"
  warn "  OAuth client secret (App settings → 'Generate a new client secret'), then:"
  warn "  ${WIRE_SCRIPT} --app-slug ${APP_NAME} --env ${ENVIRONMENT} --client-secret <secret>"
fi
echo ""
