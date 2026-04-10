#!/usr/bin/env bash
# =============================================================================
# GitHub App Token Generator (Standalone)
# =============================================================================
# Generates a GitHub App installation token for private repository access.
# Can be used standalone (e.g., in CI/CD) or as the basis for the Docker image.
#
# Usage:
#   # With AWS Secrets Manager (fetches app ID + key from SM)
#   export AWS_REGION=us-east-1
#   export SECRET_GITHUB_APP_ID=deepwiki/github-app-id
#   export SECRET_GITHUB_APP_KEY=deepwiki/github-app-key
#   ./github-app-token.sh
#
#   # With direct values (no AWS needed)
#   export GITHUB_APP_ID=123456
#   export GITHUB_APP_PRIVATE_KEY="$(cat private-key.pem)"
#   ./github-app-token.sh
#
# Output:
#   Prints the installation token to stdout (last line).
#   All log messages go to stderr.
# =============================================================================

set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
SECRET_GITHUB_APP_ID="${SECRET_GITHUB_APP_ID:-deepwiki/github-app-id}"
SECRET_GITHUB_APP_KEY="${SECRET_GITHUB_APP_KEY:-deepwiki/github-app-key}"
GITHUB_API_URL="${GITHUB_API_URL:-https://api.github.com}"

# Direct values override Secrets Manager
GITHUB_APP_ID="${GITHUB_APP_ID:-}"
GITHUB_APP_PRIVATE_KEY="${GITHUB_APP_PRIVATE_KEY:-}"

log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" >&2; }

base64url() { openssl base64 -e -A | tr '+/' '-_' | tr -d '='; }

fetch_secret() {
  aws secretsmanager get-secret-value \
    --region "$AWS_REGION" \
    --secret-id "$1" \
    --query 'SecretString' \
    --output text
}

generate_jwt() {
  local app_id="$1" private_key_pem="$2"
  local now iat exp header payload signature key_file

  now=$(date +%s)
  iat=$((now - 60))
  exp=$((now + 600))

  header=$(echo -n '{"alg":"RS256","typ":"JWT"}' | base64url)
  payload=$(echo -n "{\"iat\":${iat},\"exp\":${exp},\"iss\":\"${app_id}\"}" | base64url)

  key_file=$(mktemp)
  echo "$private_key_pem" > "$key_file"
  signature=$(echo -n "${header}.${payload}" | openssl dgst -sha256 -sign "$key_file" | base64url)
  rm -f "$key_file"

  echo "${header}.${payload}.${signature}"
}

get_installation_id() {
  local jwt="$1" response
  response=$(curl -sf \
    -H "Authorization: Bearer $jwt" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "${GITHUB_API_URL}/app/installations")

  echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])"
}

generate_installation_token() {
  local jwt="$1" installation_id="$2" response
  response=$(curl -sf -X POST \
    -H "Authorization: Bearer $jwt" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "${GITHUB_API_URL}/app/installations/${installation_id}/access_tokens")

  echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])"
}

main() {
  local app_id private_key jwt installation_id token

  # Get credentials: direct env vars or Secrets Manager
  if [[ -n "$GITHUB_APP_ID" && -n "$GITHUB_APP_PRIVATE_KEY" ]]; then
    log "Using direct environment variables for GitHub App credentials"
    app_id="$GITHUB_APP_ID"
    private_key="$GITHUB_APP_PRIVATE_KEY"
  else
    log "Fetching GitHub App credentials from Secrets Manager..."
    app_id=$(fetch_secret "$SECRET_GITHUB_APP_ID")
    private_key=$(fetch_secret "$SECRET_GITHUB_APP_KEY")
  fi

  log "Generating JWT for App ID: ${app_id}..."
  jwt=$(generate_jwt "$app_id" "$private_key")

  log "Getting installation ID..."
  installation_id=$(get_installation_id "$jwt")
  log "Installation ID: ${installation_id}"

  log "Creating installation access token..."
  token=$(generate_installation_token "$jwt" "$installation_id")

  log "Token generated successfully"
  echo "$token"
}

main "$@"
