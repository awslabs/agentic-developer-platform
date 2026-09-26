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
#   GITHUB_APP_OWNER              - REQUIRED. Org/user whose installation the
#                                   token is for (falls back to REPO_OWNER).
#                                   Without it we would mint a token against an
#                                   arbitrary installation — i.e. another
#                                   tenant's repositories (issue #4071).
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

# Target org/user whose installation the token is minted for. Required — see
# get_installation_id(). Falls back to REPO_OWNER for callers that export it.
GITHUB_APP_OWNER="${GITHUB_APP_OWNER:-${REPO_OWNER:-}}"

log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" >&2; }

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
generate_jwt() (
  local app_id="$1" private_key_pem="$2"
  local now iat exp header payload signature key_file

  now=$(date +%s)
  iat=$((now - 60))      # Allow 60s clock skew
  exp=$((now + 600))     # JWT valid for 10 minutes (GitHub max)

  header=$(echo -n '{"alg":"RS256","typ":"JWT"}' | base64url)
  payload=$(echo -n "{\"iat\":${iat},\"exp\":${exp},\"iss\":\"${app_id}\"}" | base64url)

  key_file=$(mktemp) || return 1
  trap 'rm -f "$key_file"' EXIT
  printf '%s' "$private_key_pem" > "$key_file" || return 1
  signature=$(printf '%s' "${header}.${payload}" | openssl dgst -sha256 -sign "$key_file" | base64url) || return 1

  printf '%s\n' "${header}.${payload}.${signature}"
)

# Resolve the installation id for GITHUB_APP_OWNER. Issue #4071: this used to
# return installations[0] — an arbitrary install once the App serves more than
# one org, which yields a token scoped to somebody else's repositories.
get_installation_id() {
  local jwt="$1" response
  response=$(curl -sf \
    -H "Authorization: Bearer $jwt" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "${GITHUB_API_URL}/app/installations") || return 1

  OWNER="$GITHUB_APP_OWNER" python3 -c '
import json, os, sys
owner = os.environ["OWNER"].lower()
installations = json.load(sys.stdin)
if not isinstance(installations, list):
    sys.exit("Invalid installation response")
matches = [inst for inst in installations if isinstance(inst, dict)
           and isinstance(inst.get("account"), dict)
           and str(inst["account"].get("login", "")).lower() == owner]
if len(matches) != 1:
    sys.exit("Expected exactly one installation for configured owner")
installation_id = matches[0].get("id")
if type(installation_id) is not int or installation_id <= 0:
    sys.exit("Invalid installation ID")
print(installation_id)
' <<< "$response"
}

# Create an installation access token
generate_installation_token() {
  local jwt="$1" installation_id="$2" response token
  response=$(curl -sf -X POST \
    -H "Authorization: Bearer $jwt" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "${GITHUB_API_URL}/app/installations/${installation_id}/access_tokens") || return 1

  token=$(python3 -c '
import json, re, sys
try:
    response = json.load(sys.stdin)
    token = response.get("token") if isinstance(response, dict) else None
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_]+", token):
        raise ValueError("invalid token")
except (ValueError, TypeError):
    sys.exit("Invalid installation token response")
print(token)
' <<< "$response") || return 1
  log "Token generated"
  printf '%s\n' "$token"
}

# Replace only a complete private file. Failed refreshes must retain the previous
# usable token, and readers must never see a partially written credential.
write_private_file() (
  local destination="$1" value="$2" temporary
  mkdir -p "$(dirname "$destination")" || return 1
  temporary=$(mktemp "${destination}.XXXXXX") || return 1
  trap 'rm -f "$temporary"' EXIT
  chmod 600 "$temporary" || return 1
  printf '%s' "$value" > "$temporary" || return 1
  mv -fT "$temporary" "$destination" || return 1
)

# Fetch Gateway API key from Secrets Manager and write to file
fetch_gateway_secret() {
  log "Fetching Gateway API key from Secrets Manager..."
  local gateway_key
  gateway_key=$(fetch_secret "$SECRET_GATEWAY_API_KEY") || return 1

  write_private_file "${SECRETS_DIR}/gateway-api-key" "$gateway_key" || return 1
  log "Gateway API key written to ${SECRETS_DIR}/gateway-api-key"
}

# Full flow: fetch credentials, generate JWT, get installation token
generate_and_write_token() {
  log "Fetching GitHub App credentials from Secrets Manager..."
  local app_id private_key jwt installation_id token

  app_id=$(fetch_secret "$SECRET_GITHUB_APP_ID") || return 1
  private_key=$(fetch_secret "$SECRET_GITHUB_APP_KEY") || return 1

  log "Generating JWT for GitHub App..."
  jwt=$(generate_jwt "$app_id" "$private_key") || return 1

  log "Getting installation ID for owner ${GITHUB_APP_OWNER}..."
  installation_id=$(get_installation_id "$jwt") || return 1
  log "Installation ID: ${installation_id}"

  log "Creating installation access token..."
  token=$(generate_installation_token "$jwt" "$installation_id") || return 1

  write_private_file "$GITHUB_TOKEN_PATH" "$token" || return 1
  log "Token written to ${GITHUB_TOKEN_PATH}"
}

main() {
  log "=== GitHub App Token Generator ==="
  log "Mode: $([ "$SIDECAR_MODE" = "true" ] && echo "sidecar (continuous)" || echo "init (one-shot)")"
  log "Region: ${AWS_REGION}"

  if [[ -z "$GITHUB_APP_OWNER" ]]; then
    log "ERROR: GITHUB_APP_OWNER (or REPO_OWNER) must be set to the org/user this token is for."
    log "       Minting against an arbitrary installation would produce a token for another tenant."
    exit 1
  fi
  log "Owner: ${GITHUB_APP_OWNER}"

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
