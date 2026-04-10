#!/usr/bin/env bash
# =============================================================================
# GitHub App Token Generator & Refresher
# =============================================================================
# Generates GitHub App installation tokens for private repository access.
# Also fetches the gateway API key from Secrets Manager.
#
# Modes:
#   Init (SIDECAR_MODE=false): Generate token + fetch secrets, then exit
#   Sidecar (SIDECAR_MODE=true): Continuously refresh token before expiry
#
# Required environment variables:
#   AWS_REGION                    - AWS region (default: us-east-1)
#   SECRET_GITHUB_APP_ID          - Secrets Manager name for GitHub App ID
#   SECRET_GITHUB_APP_KEY         - Secrets Manager name for GitHub App private key
#   SECRET_GATEWAY_API_KEY        - Secrets Manager name for Gateway API key
#   GITHUB_TOKEN_PATH             - File path to write the GitHub token
#   SECRETS_DIR                   - Directory to write fetched secrets
#   SIDECAR_MODE                  - "true" for continuous refresh, "false" for one-shot
#   GITHUB_TOKEN_REFRESH_INTERVAL - Seconds between refreshes in sidecar mode
# =============================================================================

set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
SECRET_GITHUB_APP_ID="${SECRET_GITHUB_APP_ID:-deepwiki/github-app-id}"
SECRET_GITHUB_APP_KEY="${SECRET_GITHUB_APP_KEY:-deepwiki/github-app-key}"
SECRET_GATEWAY_API_KEY="${SECRET_GATEWAY_API_KEY:-deepwiki/gateway-api-key}"
GITHUB_TOKEN_PATH="${GITHUB_TOKEN_PATH:-/shared/github-token}"
SECRETS_DIR="${SECRETS_DIR:-/secrets}"
GITHUB_TOKEN_REFRESH_INTERVAL="${GITHUB_TOKEN_REFRESH_INTERVAL:-3000}"
SIDECAR_MODE="${SIDECAR_MODE:-false}"
GITHUB_API_URL="${GITHUB_API_URL:-https://api.github.com}"

log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }

# Base64url encoding (no padding, URL-safe alphabet)
base64url() { openssl base64 -e -A | tr '+/' '-_' | tr -d '='; }

# Fetch a secret value from AWS Secrets Manager
fetch_secret() {
  local secret_id="$1"
  aws secretsmanager get-secret-value \
    --region "$AWS_REGION" \
    --secret-id "$secret_id" \
    --query 'SecretString' \
    --output text
}

# Generate a JWT signed with the GitHub App private key
generate_jwt() {
  local app_id="$1" private_key_pem="$2"
  local now iat exp header payload signature key_file

  now=$(date +%s)
  iat=$((now - 60))      # Allow 60s clock skew
  exp=$((now + 600))     # JWT valid for 10 minutes (GitHub max)

  header=$(echo -n '{"alg":"RS256","typ":"JWT"}' | base64url)
  payload=$(echo -n "{\"iat\":${iat},\"exp\":${exp},\"iss\":\"${app_id}\"}" | base64url)

  key_file=$(mktemp)
  echo "$private_key_pem" > "$key_file"
  signature=$(echo -n "${header}.${payload}" | openssl dgst -sha256 -sign "$key_file" | base64url)
  rm -f "$key_file"

  echo "${header}.${payload}.${signature}"
}

# Get the first installation ID for the GitHub App
get_installation_id() {
  local jwt="$1" response
  response=$(curl -sf \
    -H "Authorization: Bearer $jwt" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "${GITHUB_API_URL}/app/installations")

  echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])"
}

# Create an installation access token
generate_installation_token() {
  local jwt="$1" installation_id="$2" response token expires_at
  response=$(curl -sf -X POST \
    -H "Authorization: Bearer $jwt" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "${GITHUB_API_URL}/app/installations/${installation_id}/access_tokens")

  token=$(echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")
  expires_at=$(echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin)['expires_at'])" 2>/dev/null || echo "unknown")
  log "Token generated (expires: ${expires_at})"
  echo "$token"
}

# Fetch Gateway API key from Secrets Manager and write to file
fetch_gateway_secret() {
  log "Fetching Gateway API key from Secrets Manager..."
  local gateway_key
  gateway_key=$(fetch_secret "$SECRET_GATEWAY_API_KEY")

  mkdir -p "$SECRETS_DIR"
  echo -n "$gateway_key" > "${SECRETS_DIR}/gateway-api-key"
  chmod 600 "${SECRETS_DIR}/gateway-api-key"
  log "Gateway API key written to ${SECRETS_DIR}/gateway-api-key"
}

# Full flow: fetch credentials, generate JWT, get installation token
generate_and_write_token() {
  log "Fetching GitHub App credentials from Secrets Manager..."
  local app_id private_key jwt installation_id token

  app_id=$(fetch_secret "$SECRET_GITHUB_APP_ID")
  private_key=$(fetch_secret "$SECRET_GITHUB_APP_KEY")

  log "Generating JWT for GitHub App..."
  jwt=$(generate_jwt "$app_id" "$private_key")

  log "Getting installation ID..."
  installation_id=$(get_installation_id "$jwt")
  log "Installation ID: ${installation_id}"

  log "Creating installation access token..."
  token=$(generate_installation_token "$jwt" "$installation_id")

  mkdir -p "$(dirname "$GITHUB_TOKEN_PATH")"
  echo -n "$token" > "$GITHUB_TOKEN_PATH"
  chmod 600 "$GITHUB_TOKEN_PATH"
  log "Token written to ${GITHUB_TOKEN_PATH}"
}

main() {
  log "=== GitHub App Token Generator ==="
  log "Mode: $([ "$SIDECAR_MODE" = "true" ] && echo "sidecar (continuous)" || echo "init (one-shot)")"
  log "Region: ${AWS_REGION}"

  # Always fetch gateway API key on init
  fetch_gateway_secret

  # Generate GitHub token
  generate_and_write_token

  if [[ "$SIDECAR_MODE" == "true" ]]; then
    log "Entering refresh loop (interval: ${GITHUB_TOKEN_REFRESH_INTERVAL}s)..."
    while true; do
      sleep "$GITHUB_TOKEN_REFRESH_INTERVAL"
      log "Refreshing GitHub token..."
      if generate_and_write_token; then
        log "Token refreshed successfully"
      else
        log "WARNING: Token refresh failed, will retry next cycle"
      fi
    done
  fi

  log "=== Init complete ==="
}

main "$@"
