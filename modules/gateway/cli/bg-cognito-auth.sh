#!/usr/bin/env bash
#
# Bedrock Gateway Cognito Authentication CLI
#
# This script authenticates users with AWS Cognito and obtains temporary
# AWS credentials for accessing the Bedrock Gateway.
#
# Usage:
#   ./bg-cognito-auth.sh login --gateway-url https://gateway.company.com
#   ./bg-cognito-auth.sh refresh
#   ./bg-cognito-auth.sh logout
#   ./bg-cognito-auth.sh status
#
# After authentication, use Claude Code with:
#   CLAUDE_CODE_USE_BEDROCK=1 claude

set -euo pipefail

# Configuration file locations
CONFIG_DIR="${HOME}/.bedrock-gateway"
CONFIG_FILE="${CONFIG_DIR}/config.json"
TOKEN_FILE="${CONFIG_DIR}/tokens.json"
AWS_CREDENTIALS_FILE="${HOME}/.aws/credentials"
AWS_CONFIG_FILE="${HOME}/.aws/config"
PROFILE_NAME="bedrock-gateway"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Print colored output
print_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
print_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
print_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Check for required dependencies
check_dependencies() {
    local missing_deps=()

    if ! command -v aws &> /dev/null; then
        missing_deps+=("aws-cli")
    fi

    if ! command -v jq &> /dev/null; then
        missing_deps+=("jq")
    fi

    if [ ${#missing_deps[@]} -ne 0 ]; then
        print_error "Missing dependencies: ${missing_deps[*]}"
        echo "Please install the missing dependencies and try again."
        exit 1
    fi
}

# Initialize configuration directory
init_config() {
    mkdir -p "${CONFIG_DIR}"
    chmod 700 "${CONFIG_DIR}"

    # Create AWS credentials directory if not exists
    mkdir -p "$(dirname "${AWS_CREDENTIALS_FILE}")"
}

# Save configuration
save_config() {
    local gateway_url="$1"
    local user_pool_id="$2"
    local client_id="$3"
    local identity_pool_id="$4"
    local region="$5"

    cat > "${CONFIG_FILE}" << EOF
{
    "gateway_url": "${gateway_url}",
    "user_pool_id": "${user_pool_id}",
    "client_id": "${client_id}",
    "identity_pool_id": "${identity_pool_id}",
    "region": "${region}"
}
EOF
    chmod 600 "${CONFIG_FILE}"
}

# Load configuration
load_config() {
    if [ ! -f "${CONFIG_FILE}" ]; then
        print_error "No configuration found. Please run 'login' first."
        exit 1
    fi

    # Export config values
    GATEWAY_URL=$(jq -r '.gateway_url' "${CONFIG_FILE}")
    USER_POOL_ID=$(jq -r '.user_pool_id' "${CONFIG_FILE}")
    CLIENT_ID=$(jq -r '.client_id' "${CONFIG_FILE}")
    IDENTITY_POOL_ID=$(jq -r '.identity_pool_id' "${CONFIG_FILE}")
    REGION=$(jq -r '.region' "${CONFIG_FILE}")
}

# Save tokens
save_tokens() {
    local id_token="$1"
    local access_token="$2"
    local refresh_token="$3"
    local expires_in="$4"

    local expires_at=$(($(date +%s) + expires_in))

    cat > "${TOKEN_FILE}" << EOF
{
    "id_token": "${id_token}",
    "access_token": "${access_token}",
    "refresh_token": "${refresh_token}",
    "expires_at": ${expires_at}
}
EOF
    chmod 600 "${TOKEN_FILE}"
}

# Load tokens
load_tokens() {
    if [ ! -f "${TOKEN_FILE}" ]; then
        return 1
    fi

    ID_TOKEN=$(jq -r '.id_token' "${TOKEN_FILE}")
    ACCESS_TOKEN=$(jq -r '.access_token' "${TOKEN_FILE}")
    REFRESH_TOKEN=$(jq -r '.refresh_token' "${TOKEN_FILE}")
    EXPIRES_AT=$(jq -r '.expires_at' "${TOKEN_FILE}")

    return 0
}

# Check if tokens are expired
tokens_expired() {
    if ! load_tokens; then
        return 0  # No tokens = expired
    fi

    local current_time=$(date +%s)
    if [ "${current_time}" -ge "${EXPIRES_AT}" ]; then
        return 0  # Expired
    fi
    return 1  # Not expired
}

# Prompt for username/password and authenticate
authenticate_user() {
    local username password

    echo ""
    print_info "Bedrock Gateway Authentication"
    echo "--------------------------------"
    read -p "Username (email): " username
    read -sp "Password: " password
    echo ""

    print_info "Authenticating with Cognito..."

    # Initiate authentication with Cognito
    local auth_result
    auth_result=$(aws cognito-idp initiate-auth \
        --auth-flow USER_PASSWORD_AUTH \
        --client-id "${CLIENT_ID}" \
        --auth-parameters "USERNAME=${username},PASSWORD=${password}" \
        --region "${REGION}" \
        2>&1) || {
        print_error "Authentication failed: ${auth_result}"
        return 1
    }

    # Check if we need to respond to a challenge (e.g., NEW_PASSWORD_REQUIRED)
    local challenge_name
    challenge_name=$(echo "${auth_result}" | jq -r '.ChallengeName // empty')

    if [ -n "${challenge_name}" ]; then
        case "${challenge_name}" in
            "NEW_PASSWORD_REQUIRED")
                print_warning "Password change required."
                read -sp "New Password: " new_password
                echo ""
                read -sp "Confirm New Password: " confirm_password
                echo ""

                if [ "${new_password}" != "${confirm_password}" ]; then
                    print_error "Passwords do not match."
                    return 1
                fi

                local session
                session=$(echo "${auth_result}" | jq -r '.Session')

                auth_result=$(aws cognito-idp respond-to-auth-challenge \
                    --client-id "${CLIENT_ID}" \
                    --challenge-name NEW_PASSWORD_REQUIRED \
                    --session "${session}" \
                    --challenge-responses "USERNAME=${username},NEW_PASSWORD=${new_password}" \
                    --region "${REGION}" \
                    2>&1) || {
                    print_error "Password change failed: ${auth_result}"
                    return 1
                }
                ;;
            *)
                print_error "Unsupported challenge: ${challenge_name}"
                return 1
                ;;
        esac
    fi

    # Extract tokens from authentication result
    local id_token access_token refresh_token expires_in
    id_token=$(echo "${auth_result}" | jq -r '.AuthenticationResult.IdToken')
    access_token=$(echo "${auth_result}" | jq -r '.AuthenticationResult.AccessToken')
    refresh_token=$(echo "${auth_result}" | jq -r '.AuthenticationResult.RefreshToken')
    expires_in=$(echo "${auth_result}" | jq -r '.AuthenticationResult.ExpiresIn')

    if [ -z "${id_token}" ] || [ "${id_token}" = "null" ]; then
        print_error "Failed to obtain tokens from Cognito."
        return 1
    fi

    save_tokens "${id_token}" "${access_token}" "${refresh_token}" "${expires_in}"
    print_success "Authentication successful!"

    return 0
}

# Refresh tokens using refresh token
refresh_tokens() {
    load_config

    if ! load_tokens; then
        print_error "No tokens found. Please run 'login' first."
        return 1
    fi

    print_info "Refreshing tokens..."

    local auth_result
    auth_result=$(aws cognito-idp initiate-auth \
        --auth-flow REFRESH_TOKEN_AUTH \
        --client-id "${CLIENT_ID}" \
        --auth-parameters "REFRESH_TOKEN=${REFRESH_TOKEN}" \
        --region "${REGION}" \
        2>&1) || {
        print_error "Token refresh failed: ${auth_result}"
        print_warning "Please run 'login' to re-authenticate."
        return 1
    }

    local id_token access_token expires_in
    id_token=$(echo "${auth_result}" | jq -r '.AuthenticationResult.IdToken')
    access_token=$(echo "${auth_result}" | jq -r '.AuthenticationResult.AccessToken')
    expires_in=$(echo "${auth_result}" | jq -r '.AuthenticationResult.ExpiresIn')

    # Refresh token may or may not be returned
    local new_refresh_token
    new_refresh_token=$(echo "${auth_result}" | jq -r '.AuthenticationResult.RefreshToken // empty')
    if [ -z "${new_refresh_token}" ]; then
        new_refresh_token="${REFRESH_TOKEN}"
    fi

    save_tokens "${id_token}" "${access_token}" "${new_refresh_token}" "${expires_in}"
    print_success "Tokens refreshed successfully!"

    return 0
}

# Exchange Cognito tokens for AWS credentials via Identity Pool
exchange_for_aws_credentials() {
    load_config

    if ! load_tokens; then
        print_error "No tokens found. Please run 'login' first."
        return 1
    fi

    print_info "Exchanging tokens for AWS credentials..."

    # Get identity ID from Cognito Identity Pool
    local provider_name="cognito-idp.${REGION}.amazonaws.com/${USER_POOL_ID}"
    local identity_result
    identity_result=$(aws cognito-identity get-id \
        --identity-pool-id "${IDENTITY_POOL_ID}" \
        --logins "${provider_name}=${ID_TOKEN}" \
        --region "${REGION}" \
        2>&1) || {
        print_error "Failed to get identity: ${identity_result}"
        return 1
    }

    local identity_id
    identity_id=$(echo "${identity_result}" | jq -r '.IdentityId')

    if [ -z "${identity_id}" ] || [ "${identity_id}" = "null" ]; then
        print_error "Failed to obtain identity ID."
        return 1
    fi

    # Get credentials for the identity
    local credentials_result
    credentials_result=$(aws cognito-identity get-credentials-for-identity \
        --identity-id "${identity_id}" \
        --logins "${provider_name}=${ID_TOKEN}" \
        --region "${REGION}" \
        2>&1) || {
        print_error "Failed to get credentials: ${credentials_result}"
        return 1
    }

    local access_key_id secret_access_key session_token expiration
    access_key_id=$(echo "${credentials_result}" | jq -r '.Credentials.AccessKeyId')
    secret_access_key=$(echo "${credentials_result}" | jq -r '.Credentials.SecretKey')
    session_token=$(echo "${credentials_result}" | jq -r '.Credentials.SessionToken')
    expiration=$(echo "${credentials_result}" | jq -r '.Credentials.Expiration')

    if [ -z "${access_key_id}" ] || [ "${access_key_id}" = "null" ]; then
        print_error "Failed to obtain AWS credentials."
        return 1
    fi

    # Write credentials to AWS credentials file
    write_aws_credentials "${access_key_id}" "${secret_access_key}" "${session_token}"

    print_success "AWS credentials obtained successfully!"
    print_info "Credentials expire at: ${expiration}"

    return 0
}

# Write AWS credentials to credentials file
write_aws_credentials() {
    local access_key_id="$1"
    local secret_access_key="$2"
    local session_token="$3"

    # Backup existing credentials file
    if [ -f "${AWS_CREDENTIALS_FILE}" ]; then
        # Remove existing bedrock-gateway profile if present
        local temp_file
        temp_file=$(mktemp)

        # Use awk to filter out the existing profile
        awk -v profile="[${PROFILE_NAME}]" '
            BEGIN { skip = 0 }
            /^\[/ { skip = ($0 == profile) }
            !skip { print }
        ' "${AWS_CREDENTIALS_FILE}" > "${temp_file}"

        mv "${temp_file}" "${AWS_CREDENTIALS_FILE}"
    fi

    # Append the new profile
    cat >> "${AWS_CREDENTIALS_FILE}" << EOF

[${PROFILE_NAME}]
aws_access_key_id = ${access_key_id}
aws_secret_access_key = ${secret_access_key}
aws_session_token = ${session_token}
EOF

    chmod 600 "${AWS_CREDENTIALS_FILE}"

    # Also update AWS config with region
    if [ -f "${AWS_CONFIG_FILE}" ]; then
        # Remove existing profile config
        local temp_file
        temp_file=$(mktemp)

        awk -v profile="[profile ${PROFILE_NAME}]" '
            BEGIN { skip = 0 }
            /^\[/ { skip = ($0 == profile) }
            !skip { print }
        ' "${AWS_CONFIG_FILE}" > "${temp_file}"

        mv "${temp_file}" "${AWS_CONFIG_FILE}"
    fi

    cat >> "${AWS_CONFIG_FILE}" << EOF

[profile ${PROFILE_NAME}]
region = ${REGION}
output = json
EOF

    chmod 600 "${AWS_CONFIG_FILE}"
}

# Login command
cmd_login() {
    local gateway_url=""
    local user_pool_id=""
    local client_id=""
    local identity_pool_id=""
    local region="us-east-1"

    # Parse arguments
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --gateway-url)
                gateway_url="$2"
                shift 2
                ;;
            --user-pool-id)
                user_pool_id="$2"
                shift 2
                ;;
            --client-id)
                client_id="$2"
                shift 2
                ;;
            --identity-pool-id)
                identity_pool_id="$2"
                shift 2
                ;;
            --region)
                region="$2"
                shift 2
                ;;
            *)
                print_error "Unknown option: $1"
                usage
                exit 1
                ;;
        esac
    done

    # Check required arguments
    if [ -z "${gateway_url}" ]; then
        print_error "Gateway URL is required (--gateway-url)"
        exit 1
    fi

    # Try to auto-discover Cognito settings from gateway if not provided
    if [ -z "${user_pool_id}" ] || [ -z "${client_id}" ] || [ -z "${identity_pool_id}" ]; then
        print_info "Attempting to discover Cognito settings from gateway..."

        local discovery_url="${gateway_url}/.well-known/cognito-config"
        local discovery_result
        discovery_result=$(curl -s "${discovery_url}" 2>/dev/null) || true

        if [ -n "${discovery_result}" ] && echo "${discovery_result}" | jq . >/dev/null 2>&1; then
            user_pool_id=${user_pool_id:-$(echo "${discovery_result}" | jq -r '.user_pool_id // empty')}
            client_id=${client_id:-$(echo "${discovery_result}" | jq -r '.client_id // empty')}
            identity_pool_id=${identity_pool_id:-$(echo "${discovery_result}" | jq -r '.identity_pool_id // empty')}
            region=${region:-$(echo "${discovery_result}" | jq -r '.region // empty')}
            print_success "Discovered Cognito configuration from gateway."
        fi
    fi

    # Prompt for missing values
    if [ -z "${user_pool_id}" ]; then
        read -p "Cognito User Pool ID: " user_pool_id
    fi
    if [ -z "${client_id}" ]; then
        read -p "Cognito Client ID: " client_id
    fi
    if [ -z "${identity_pool_id}" ]; then
        read -p "Cognito Identity Pool ID: " identity_pool_id
    fi

    # Save configuration
    save_config "${gateway_url}" "${user_pool_id}" "${client_id}" "${identity_pool_id}" "${region}"

    # Load the saved config
    load_config

    # Authenticate user
    if ! authenticate_user; then
        exit 1
    fi

    # Exchange for AWS credentials
    if ! exchange_for_aws_credentials; then
        exit 1
    fi

    # Print usage instructions
    echo ""
    print_success "Setup complete!"
    echo ""
    echo "To use the Bedrock Gateway with Claude Code:"
    echo ""
    echo "  export AWS_PROFILE=${PROFILE_NAME}"
    echo "  export ANTHROPIC_BEDROCK_BASE_URL=${gateway_url}"
    echo "  CLAUDE_CODE_USE_BEDROCK=1 claude"
    echo ""
    echo "Or add to your shell profile (.bashrc/.zshrc):"
    echo ""
    echo "  # Bedrock Gateway"
    echo "  export AWS_PROFILE=${PROFILE_NAME}"
    echo "  export ANTHROPIC_BEDROCK_BASE_URL=${gateway_url}"
    echo ""
}

# Refresh command
cmd_refresh() {
    if ! refresh_tokens; then
        exit 1
    fi

    if ! exchange_for_aws_credentials; then
        exit 1
    fi

    print_success "Credentials refreshed successfully!"
}

# Logout command
cmd_logout() {
    print_info "Logging out..."

    # Remove token file
    if [ -f "${TOKEN_FILE}" ]; then
        rm -f "${TOKEN_FILE}"
    fi

    # Remove credentials profile
    if [ -f "${AWS_CREDENTIALS_FILE}" ]; then
        local temp_file
        temp_file=$(mktemp)

        awk -v profile="[${PROFILE_NAME}]" '
            BEGIN { skip = 0 }
            /^\[/ { skip = ($0 == profile) }
            !skip { print }
        ' "${AWS_CREDENTIALS_FILE}" > "${temp_file}"

        mv "${temp_file}" "${AWS_CREDENTIALS_FILE}"
    fi

    print_success "Logged out successfully."
}

# Status command
cmd_status() {
    print_info "Bedrock Gateway Authentication Status"
    echo "--------------------------------------"

    if [ ! -f "${CONFIG_FILE}" ]; then
        echo "Status: Not configured"
        echo "Run 'bg-cognito-auth.sh login --gateway-url <url>' to configure."
        return
    fi

    load_config
    echo "Gateway URL: ${GATEWAY_URL}"
    echo "Region: ${REGION}"
    echo "User Pool ID: ${USER_POOL_ID}"
    echo ""

    if load_tokens; then
        local current_time
        current_time=$(date +%s)

        if [ "${current_time}" -lt "${EXPIRES_AT}" ]; then
            local remaining
            remaining=$((EXPIRES_AT - current_time))
            echo "Token Status: Valid"
            echo "Expires in: $((remaining / 60)) minutes"
        else
            echo "Token Status: Expired"
            echo "Run 'bg-cognito-auth.sh refresh' to refresh tokens."
        fi
    else
        echo "Token Status: No tokens"
        echo "Run 'bg-cognito-auth.sh login' to authenticate."
    fi

    echo ""

    # Check AWS credentials
    if aws sts get-caller-identity --profile "${PROFILE_NAME}" &>/dev/null; then
        echo "AWS Credentials: Valid"
        local identity
        identity=$(aws sts get-caller-identity --profile "${PROFILE_NAME}" --output json)
        echo "  ARN: $(echo "${identity}" | jq -r '.Arn')"
    else
        echo "AWS Credentials: Invalid or expired"
        echo "Run 'bg-cognito-auth.sh refresh' to refresh credentials."
    fi
}

# Token command - outputs the current access token for use as apiKeyHelper (Issue #119)
cmd_token() {
    # Load configuration
    if [ ! -f "${CONFIG_FILE}" ]; then
        echo "Not configured. Run: bg-cognito-auth.sh login" >&2
        exit 1
    fi
    load_config

    # Load saved tokens
    if ! load_tokens; then
        echo "Not logged in. Run: bg-cognito-auth.sh login" >&2
        exit 1
    fi

    # Check if tokens are expired
    local current_time
    current_time=$(date +%s)

    # Add a 5-minute buffer to refresh before expiry
    local buffer=300
    local expiry_with_buffer=$((EXPIRES_AT - buffer))

    if [ "${current_time}" -ge "${expiry_with_buffer}" ]; then
        # Token expired or about to expire - try to refresh
        if ! refresh_tokens >/dev/null 2>&1; then
            echo "Token expired. Run: bg-cognito-auth.sh login" >&2
            exit 1
        fi
        # Reload tokens after refresh
        load_tokens
    fi

    # Output just the access token to stdout (no newline for clean output)
    # This is used by Claude Code's apiKeyHelper configuration
    printf "%s" "${ACCESS_TOKEN}"
}

# Usage information
usage() {
    cat << EOF
Bedrock Gateway Cognito Authentication CLI

Usage:
    $(basename "$0") <command> [options]

Commands:
    login       Authenticate with Cognito and obtain AWS credentials
    refresh     Refresh tokens and AWS credentials
    logout      Remove stored tokens and credentials
    status      Show current authentication status
    token       Output current access token (for apiKeyHelper - Issue #119)

Login Options:
    --gateway-url <url>       Gateway URL (required)
    --user-pool-id <id>       Cognito User Pool ID
    --client-id <id>          Cognito Client ID
    --identity-pool-id <id>   Cognito Identity Pool ID
    --region <region>         AWS region (default: us-east-1)

Examples:
    # Interactive login (discovers settings from gateway)
    $(basename "$0") login --gateway-url https://gateway.company.com

    # Login with explicit settings
    $(basename "$0") login --gateway-url https://gateway.company.com \\
        --user-pool-id us-east-1_xxxxx \\
        --client-id xxxxxxxxxxxxx \\
        --identity-pool-id us-east-1:xxxxx

    # Refresh credentials
    $(basename "$0") refresh

    # Check status
    $(basename "$0") status

    # Get access token for apiKeyHelper (Issue #119)
    $(basename "$0") token

After authentication, use Claude Code with:
    CLAUDE_CODE_USE_BEDROCK=1 claude

Claude Code Configuration (Issue #119):
    Add to ~/.claude/settings.json:
    {
      "env": {
        "ANTHROPIC_BEDROCK_BASE_URL": "https://your-gateway.cloudfront.net/api",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1"
      },
      "apiKeyHelper": "~/bin/bg-cognito-auth.sh token",
      "apiKeyHelperTtlMs": 3300000
    }

EOF
}

# Main entry point
main() {
    check_dependencies
    init_config

    local command="${1:-}"
    shift || true

    case "${command}" in
        login)
            cmd_login "$@"
            ;;
        refresh)
            cmd_refresh "$@"
            ;;
        logout)
            cmd_logout "$@"
            ;;
        status)
            cmd_status "$@"
            ;;
        token)
            cmd_token "$@"
            ;;
        help|--help|-h)
            usage
            ;;
        "")
            print_error "No command specified."
            usage
            exit 1
            ;;
        *)
            print_error "Unknown command: ${command}"
            usage
            exit 1
            ;;
    esac
}

main "$@"
