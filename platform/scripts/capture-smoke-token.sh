#!/usr/bin/env bash
# capture-smoke-token.sh — One-time script to seed the Cognito refresh token
# into Secrets Manager for the post-deploy smoke test (issue #3031).
#
# Usage:
#   ./platform/scripts/capture-smoke-token.sh --environment dev
#   ./platform/scripts/capture-smoke-token.sh --environment dev --token "eyJra..."
#
# How to obtain the refresh token:
#   1. Log in to the gateway UI via GitHub OAuth (normal browser flow).
#   2. Open browser DevTools → Application → Session Storage → select the
#      gateway origin → find the key "cognito_refresh_token".
#   3. Copy the value and pass it via --token or paste when prompted.
#
# The token is stored in:
#   Secret: adp/<env>/gateway/smoke-user-refresh-token
#
# Token lifetime: Cognito refresh tokens expire after 30 days by default.
# When the smoke test emits SMOKE_TOKEN_EXPIRED, re-run this script.
set -euo pipefail

ENVIRONMENT=""
TOKEN=""

usage() {
  echo "Usage: $0 --environment <env> [--token <refresh_token>]"
  echo ""
  echo "Options:"
  echo "  --environment, -e   Target environment (dev/staging/prod)"
  echo "  --token, -t         Refresh token value (prompted if omitted)"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --environment|-e) ENVIRONMENT="$2"; shift 2 ;;
    --token|-t) TOKEN="$2"; shift 2 ;;
    --help|-h) usage ;;
    *) echo "Unknown option: $1"; usage ;;
  esac
done

if [[ -z "$ENVIRONMENT" ]]; then
  echo "ERROR: --environment is required"
  usage
fi

# Prompt for token if not provided via flag
if [[ -z "$TOKEN" ]]; then
  echo "Paste the Cognito refresh token (from browser Session Storage → cognito_refresh_token):"
  read -r -s TOKEN
  echo "(received ${#TOKEN} chars)"
fi

if [[ -z "$TOKEN" ]]; then
  echo "ERROR: Token cannot be empty."
  exit 1
fi

# Validate it looks like a JWT-ish string (refresh tokens are opaque but non-empty)
if [[ ${#TOKEN} -lt 20 ]]; then
  echo "ERROR: Token too short (${#TOKEN} chars). Expected a Cognito refresh token (~1000+ chars)."
  exit 1
fi

SECRET_NAME="adp/${ENVIRONMENT}/gateway/smoke-user-refresh-token"
REGION="${AWS_REGION:-us-east-1}"

echo "Writing refresh token to Secrets Manager:"
echo "  Secret: ${SECRET_NAME}"
echo "  Region: ${REGION}"

# Create or update the secret
if aws secretsmanager describe-secret --secret-id "$SECRET_NAME" --region "$REGION" >/dev/null 2>&1; then
  aws secretsmanager put-secret-value \
    --secret-id "$SECRET_NAME" \
    --secret-string "$TOKEN" \
    --region "$REGION"
  echo "✓ Secret updated."
else
  aws secretsmanager create-secret \
    --name "$SECRET_NAME" \
    --secret-string "$TOKEN" \
    --description "Cognito refresh token for post-deploy smoke test (issue #3031). Re-capture every 30 days." \
    --region "$REGION"
  echo "✓ Secret created."
fi

echo ""
echo "Done. The gateway-smoke workflow will now use this token."
echo "Remember: token expires in ~30 days. Re-run this script when you see SMOKE_TOKEN_EXPIRED."
