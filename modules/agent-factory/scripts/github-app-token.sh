#!/usr/bin/env bash
# =============================================================================
# GitHub App Token Generator (Standalone)
# =============================================================================
# Generates a GitHub App installation token for private repository access.
# Can be used standalone (e.g., in CI/CD) or as the basis for the Docker image.
#
# GITHUB_APP_OWNER (or REPO_OWNER) is REQUIRED: the token is minted for that
# org/user's installation specifically. Without it we would mint against an
# arbitrary installation, i.e. another tenant's repositories (issue #4071).
#
# Usage:
#   # With AWS Secrets Manager (fetches app ID + key from SM)
#   export AWS_REGION=us-east-1
#   export SECRET_GITHUB_APP_ID=deepwiki/github-app-id
#   export SECRET_GITHUB_APP_KEY=deepwiki/github-app-key
#   export GITHUB_APP_OWNER=my-org
#   ./github-app-token.sh
#
#   # With direct values (no AWS needed)
#   export GITHUB_APP_ID=123456
#   export GITHUB_APP_PRIVATE_KEY="$(cat private-key.pem)"
#   export GITHUB_APP_OWNER=my-org
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

# Target org/user whose installation the token is minted for. Required — see
# get_installation_id(). Falls back to REPO_OWNER for callers that already
# export it (agent runs, GitHub Actions).
GITHUB_APP_OWNER="${GITHUB_APP_OWNER:-${REPO_OWNER:-}}"

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

# Resolve the installation id for GITHUB_APP_OWNER. Issue #4071: this used to
# return installations[0] — an arbitrary install once the App serves more than
# one org, which yields a token scoped to somebody else's repositories.
get_installation_id() {
  local jwt="$1" response
  response=$(curl -sf \
    -H "Authorization: Bearer $jwt" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "${GITHUB_API_URL}/app/installations")

  OWNER="$GITHUB_APP_OWNER" python3 -c '
import json, os, sys
owner = os.environ["OWNER"].lower()
installations = json.load(sys.stdin)
for inst in installations:
    if inst.get("account", {}).get("login", "").lower() == owner:
        print(inst["id"])
        sys.exit(0)
found = ", ".join(i.get("account", {}).get("login", "?") for i in installations) or "none"
sys.exit(f"No installation found for owner {owner!r}. App is installed on: {found}")
' <<< "$response"
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

  if [[ -z "$GITHUB_APP_OWNER" ]]; then
    log "ERROR: GITHUB_APP_OWNER (or REPO_OWNER) must be set to the org/user this token is for."
    log "       Minting against an arbitrary installation would produce a token for another tenant."
    exit 1
  fi

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

  log "Getting installation ID for owner ${GITHUB_APP_OWNER}..."
  installation_id=$(get_installation_id "$jwt")
  log "Installation ID: ${installation_id}"

  log "Creating installation access token..."
  token=$(generate_installation_token "$jwt" "$installation_id")

  log "Token generated successfully"
  echo "$token"
}

main "$@"
