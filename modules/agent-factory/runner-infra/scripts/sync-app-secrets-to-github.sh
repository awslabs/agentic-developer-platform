#!/bin/bash
# =============================================================================
# Sync GitHub App Secrets to Repository
# =============================================================================
# Copies GitHub App credentials from AWS Secrets Manager to GitHub repo secrets.
#
# Usage:
#   ./sync-app-secrets-to-github.sh [--repo OWNER/REPO] [--apps APP_LIST]
#
# Examples:
#   ./sync-app-secrets-to-github.sh --repo aws-innovate/adp
#   ./sync-app-secrets-to-github.sh --repo aws-innovate/adp --apps "pm,dev"
# =============================================================================

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Defaults
REPO="${REPO:-aws-innovate/adp}"
APPS="pm,dev,ops"
AWS_REGION="${AWS_REGION:-us-west-2}"
SECRET_PREFIX="github-app"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --repo)
            REPO="$2"
            shift 2
            ;;
        --apps)
            APPS="$2"
            shift 2
            ;;
        --region)
            AWS_REGION="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [--repo OWNER/REPO] [--apps APP_LIST] [--region REGION]"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

log() {
    local level=$1
    shift
    local color=""
    case $level in
        INFO) color=$BLUE ;;
        SUCCESS) color=$GREEN ;;
        WARN) color=$YELLOW ;;
        ERROR) color=$RED ;;
    esac
    echo -e "${color}[$level]${NC} $*"
}

# Check prerequisites
command -v gh >/dev/null 2>&1 || { log ERROR "gh CLI is required"; exit 1; }
command -v aws >/dev/null 2>&1 || { log ERROR "aws CLI is required"; exit 1; }
command -v jq >/dev/null 2>&1 || { log ERROR "jq is required"; exit 1; }

# Check gh auth
if ! gh auth status >/dev/null 2>&1; then
    log ERROR "Not authenticated with gh. Run 'gh auth login' first."
    exit 1
fi

log INFO "Syncing GitHub App secrets to repository: $REPO"
log INFO "Apps: $APPS"
log INFO "AWS Region: $AWS_REGION"
echo ""

IFS=',' read -ra APP_LIST <<< "$APPS"

for app in "${APP_LIST[@]}"; do
    app=$(echo "$app" | tr '[:lower:]' '[:upper:]')  # Convert to uppercase for secret names
    app_lower=$(echo "$app" | tr '[:upper:]' '[:lower:]')  # Lowercase for AWS secret name

    log INFO "Processing ${app} app..."

    # Get secret from AWS
    SECRET_JSON=$(aws secretsmanager get-secret-value \
        --secret-id "${SECRET_PREFIX}-${app_lower}" \
        --region "$AWS_REGION" \
        --query 'SecretString' \
        --output text 2>/dev/null) || {
        log WARN "  Secret ${SECRET_PREFIX}-${app_lower} not found in AWS, skipping"
        continue
    }

    APP_ID=$(echo "$SECRET_JSON" | jq -r '.app_id')
    PRIVATE_KEY=$(echo "$SECRET_JSON" | jq -r '.pem')

    if [[ -z "$APP_ID" || "$APP_ID" == "null" ]]; then
        log WARN "  No app_id found for $app, skipping"
        continue
    fi

    # Set GitHub secrets
    log INFO "  Setting GH_APP_${app}_ID..."
    echo "$APP_ID" | gh secret set "GH_APP_${app}_ID" --repo "$REPO"

    log INFO "  Setting GH_APP_${app}_PRIVATE_KEY..."
    echo "$PRIVATE_KEY" | gh secret set "GH_APP_${app}_PRIVATE_KEY" --repo "$REPO"

    log SUCCESS "  ✓ ${app} app secrets synced"
done

echo ""
log SUCCESS "Done! Secrets synced to $REPO"
echo ""
log INFO "You can now update your workflows to use these secrets:"
echo ""
echo "  # For @agent-pm (uses PM app):"
echo "  - uses: actions/create-github-app-token@v1"
echo "    with:"
echo "      app-id: \${{ secrets.GH_APP_PM_ID }}"
echo "      private-key: \${{ secrets.GH_APP_PM_PRIVATE_KEY }}"
echo ""
echo "  # For @agent-developer (uses DEV app):"
echo "  - uses: actions/create-github-app-token@v1"
echo "    with:"
echo "      app-id: \${{ secrets.GH_APP_DEV_ID }}"
echo "      private-key: \${{ secrets.GH_APP_DEV_PRIVATE_KEY }}"
echo ""
