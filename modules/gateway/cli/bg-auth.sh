#!/usr/bin/env bash
#
# bg-auth.sh - Bedrock Gateway Authentication Helper
#
# This script exchanges AWS credentials for a BedrockGateway token.
# It supports both human users (via AWS SSO) and M2M service accounts
# (via EKS Pod Identity/IRSA).
#
# Usage:
#   bg-auth.sh                    # Uses default profile or environment credentials
#   bg-auth.sh --profile myprofile # Uses specific AWS profile
#   bg-auth.sh --gateway-url URL  # Override gateway URL
#   bg-auth.sh --debug            # Enable debug output to stderr
#
# Environment Variables:
#   BG_GATEWAY_URL    - BedrockGateway URL (required unless --gateway-url is set)
#   AWS_PROFILE       - AWS profile to use (optional)
#   AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN - Direct credentials
#
# Exit Codes:
#   0 - Success, token printed to stdout
#   1 - AWS credentials expired or invalid
#   2 - Gateway authentication failed
#   3 - Configuration error (missing gateway URL, etc.)
#   4 - Network error
#   5 - Dependency missing (curl, jq, aws)
#
# Returns:
#   On success, prints the bearer token to stdout (just the token value)
#   All diagnostic messages go to stderr
#

set -euo pipefail

# Constants
readonly VERSION="1.0.0"
readonly SCRIPT_NAME="bg-auth"

# Global variables
DEBUG=false
GATEWAY_URL="${BG_GATEWAY_URL:-}"
AWS_PROFILE_ARG=""

# Logging functions
log_debug() {
    if [[ "$DEBUG" == "true" ]]; then
        echo "[DEBUG] $*" >&2
    fi
}

log_info() {
    echo "[INFO] $*" >&2
}

log_error() {
    echo "[ERROR] $*" >&2
}

log_warn() {
    echo "[WARN] $*" >&2
}

# Print usage information
usage() {
    cat >&2 << EOF
$SCRIPT_NAME v$VERSION - Bedrock Gateway Authentication Helper

Usage: $SCRIPT_NAME [OPTIONS]

Options:
  --gateway-url URL    Override the gateway URL (default: \$BG_GATEWAY_URL)
  --profile PROFILE    Use a specific AWS profile
  --debug              Enable debug output
  -h, --help           Show this help message
  -v, --version        Show version

Environment Variables:
  BG_GATEWAY_URL       BedrockGateway URL (required unless --gateway-url is set)
  AWS_PROFILE          AWS profile to use
  AWS_ACCESS_KEY_ID    AWS access key (for direct credential usage)
  AWS_SECRET_ACCESS_KEY AWS secret key
  AWS_SESSION_TOKEN    AWS session token (required for temporary credentials)

Examples:
  # Using AWS SSO credentials
  aws sso login --profile myprofile
  export BG_GATEWAY_URL=https://gateway.example.com
  $SCRIPT_NAME --profile myprofile

  # Using environment credentials (EKS/Lambda/EC2)
  export BG_GATEWAY_URL=https://gateway.example.com
  $SCRIPT_NAME

  # With Claude Code apiKeyHelper
  # In ~/.claude/settings.json:
  # {
  #   "env": {
  #     "ANTHROPIC_BEDROCK_BASE_URL": "https://gateway.example.com",
  #     "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
  #     "CLAUDE_CODE_USE_BEDROCK": "1",
  #     "BG_GATEWAY_URL": "https://gateway.example.com"
  #   },
  #   "apiKeyHelper": "~/bin/bg-auth.sh"
  # }
EOF
}

# Check required dependencies
check_dependencies() {
    local missing=()

    for cmd in curl jq aws; do
        if ! command -v "$cmd" &>/dev/null; then
            missing+=("$cmd")
        fi
    done

    if [[ ${#missing[@]} -gt 0 ]]; then
        log_error "Missing required dependencies: ${missing[*]}"
        log_error "Please install them and try again."
        exit 5
    fi

    log_debug "All dependencies found: curl, jq, aws"
}

# Parse command line arguments
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --gateway-url)
                if [[ -z "${2:-}" ]]; then
                    log_error "--gateway-url requires a URL argument"
                    exit 3
                fi
                GATEWAY_URL="$2"
                shift 2
                ;;
            --profile)
                if [[ -z "${2:-}" ]]; then
                    log_error "--profile requires a profile name argument"
                    exit 3
                fi
                AWS_PROFILE_ARG="--profile $2"
                shift 2
                ;;
            --debug)
                DEBUG=true
                shift
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            -v|--version)
                echo "$SCRIPT_NAME v$VERSION"
                exit 0
                ;;
            *)
                log_error "Unknown option: $1"
                usage
                exit 3
                ;;
        esac
    done
}

# Validate configuration
validate_config() {
    if [[ -z "$GATEWAY_URL" ]]; then
        log_error "Gateway URL not configured."
        log_error "Set BG_GATEWAY_URL environment variable or use --gateway-url option."
        exit 3
    fi

    # Ensure URL doesn't have trailing slash
    GATEWAY_URL="${GATEWAY_URL%/}"

    log_debug "Gateway URL: $GATEWAY_URL"
}

# Get AWS credentials from the environment or profile
get_aws_credentials() {
    local sts_output

    log_debug "Attempting to get AWS credentials..."

    # First, try to get caller identity to validate credentials
    # shellcheck disable=SC2086
    if ! sts_output=$(aws sts get-caller-identity $AWS_PROFILE_ARG --output json 2>&1); then
        # Check for common error patterns
        if echo "$sts_output" | grep -qi "expired"; then
            log_error "AWS credentials expired. Run: aws sso login${AWS_PROFILE_ARG:+ $AWS_PROFILE_ARG}"
            exit 1
        elif echo "$sts_output" | grep -qi "unable to locate credentials"; then
            log_error "No AWS credentials found."
            log_error "Run: aws sso login --profile <profile> OR configure AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY"
            exit 1
        elif echo "$sts_output" | grep -qi "invalid"; then
            log_error "AWS credentials are invalid: $sts_output"
            exit 1
        else
            log_error "Failed to validate AWS credentials: $sts_output"
            exit 1
        fi
    fi

    log_debug "STS GetCallerIdentity successful"
    log_debug "Response: $sts_output"

    # Now extract the credentials
    # For environment-based credentials, they're already available
    # For profile-based credentials, we need to export them

    local access_key secret_key session_token

    # Check if credentials are in environment
    if [[ -n "${AWS_ACCESS_KEY_ID:-}" ]] && [[ -n "${AWS_SECRET_ACCESS_KEY:-}" ]]; then
        access_key="$AWS_ACCESS_KEY_ID"
        secret_key="$AWS_SECRET_ACCESS_KEY"
        session_token="${AWS_SESSION_TOKEN:-}"
        log_debug "Using credentials from environment"
    else
        # Get credentials from AWS CLI
        local creds_output
        # shellcheck disable=SC2086
        if ! creds_output=$(aws configure export-credentials $AWS_PROFILE_ARG --format env 2>&1); then
            # Fallback: try to get from credentials file or instance metadata
            log_debug "export-credentials failed, trying alternative methods..."

            # Try using aws configure get
            # shellcheck disable=SC2086
            access_key=$(aws configure get aws_access_key_id $AWS_PROFILE_ARG 2>/dev/null || echo "")
            # shellcheck disable=SC2086
            secret_key=$(aws configure get aws_secret_access_key $AWS_PROFILE_ARG 2>/dev/null || echo "")
            # shellcheck disable=SC2086
            session_token=$(aws configure get aws_session_token $AWS_PROFILE_ARG 2>/dev/null || echo "")

            if [[ -z "$access_key" ]] || [[ -z "$secret_key" ]]; then
                log_error "Could not extract AWS credentials."
                log_error "Make sure you're logged in: aws sso login${AWS_PROFILE_ARG:+ $AWS_PROFILE_ARG}"
                exit 1
            fi
        else
            # Parse the env output
            # Format: export AWS_ACCESS_KEY_ID=xxx
            access_key=$(echo "$creds_output" | grep "^export AWS_ACCESS_KEY_ID=" | sed 's/^export AWS_ACCESS_KEY_ID=//')
            secret_key=$(echo "$creds_output" | grep "^export AWS_SECRET_ACCESS_KEY=" | sed 's/^export AWS_SECRET_ACCESS_KEY=//')
            session_token=$(echo "$creds_output" | grep "^export AWS_SESSION_TOKEN=" | sed 's/^export AWS_SESSION_TOKEN=//' || echo "")
            log_debug "Extracted credentials from AWS CLI export-credentials"
        fi
    fi

    # Validate we have credentials
    if [[ -z "$access_key" ]] || [[ -z "$secret_key" ]]; then
        log_error "Failed to get AWS credentials"
        exit 1
    fi

    # Return credentials as JSON for the exchange API
    # Note: Session token is required for SSO/temporary credentials
    if [[ -z "$session_token" ]]; then
        log_warn "No session token found - this may fail if using temporary credentials"
        session_token=""
    fi

    # Output credentials as global variables (avoid subshell issues)
    AWS_CREDS_ACCESS_KEY="$access_key"
    AWS_CREDS_SECRET_KEY="$secret_key"
    AWS_CREDS_SESSION_TOKEN="$session_token"
}

# Exchange AWS credentials for a gateway token
exchange_credentials() {
    local access_key="$1"
    local secret_key="$2"
    local session_token="$3"

    local exchange_url="${GATEWAY_URL}/auth/exchange"
    local request_body
    local response
    local http_code
    local temp_file

    log_debug "Exchanging credentials at: $exchange_url"

    # Build request body
    request_body=$(jq -n \
        --arg ak "$access_key" \
        --arg sk "$secret_key" \
        --arg st "$session_token" \
        '{
            aws_access_key_id: $ak,
            aws_secret_access_key: $sk,
            aws_session_token: $st
        }')

    log_debug "Request body prepared (credentials redacted)"

    # Create temp file for response body
    temp_file=$(mktemp)
    # shellcheck disable=SC2064
    trap "rm -f '$temp_file'" EXIT

    # Make the request
    http_code=$(curl -s -w "%{http_code}" \
        -X POST \
        -H "Content-Type: application/json" \
        -d "$request_body" \
        -o "$temp_file" \
        --connect-timeout 10 \
        --max-time 30 \
        "$exchange_url" 2>&1) || {
            local curl_exit=$?
            log_error "Network error connecting to gateway (curl exit code: $curl_exit)"
            rm -f "$temp_file"
            exit 4
        }

    response=$(cat "$temp_file")
    rm -f "$temp_file"
    trap - EXIT

    log_debug "HTTP response code: $http_code"
    log_debug "Response body: $response"

    # Handle response based on HTTP code
    case "$http_code" in
        200)
            # Success - extract token
            local token
            token=$(echo "$response" | jq -r '.token // empty')

            if [[ -z "$token" ]]; then
                log_error "Invalid response from gateway: no token in response"
                log_debug "Full response: $response"
                exit 2
            fi

            # Output just the token to stdout (for apiKeyHelper)
            echo "$token"
            log_debug "Token retrieved successfully"
            ;;
        401)
            # Unauthorized - credentials invalid/expired
            local error_msg
            error_msg=$(echo "$response" | jq -r '.message // .error // "Unknown error"')
            log_error "Authentication failed: $error_msg"
            log_error "Your AWS credentials may be expired. Run: aws sso login${AWS_PROFILE_ARG:+ $AWS_PROFILE_ARG}"
            exit 1
            ;;
        403)
            # Forbidden - unknown org or unregistered service account
            local error_type error_msg
            error_type=$(echo "$response" | jq -r '.error // "forbidden"')
            error_msg=$(echo "$response" | jq -r '.message // "Access denied"')

            case "$error_type" in
                unknown_organization)
                    log_error "Your AWS account is not registered with any organization."
                    log_error "Contact your platform administrator to be onboarded."
                    ;;
                unregistered_service_account)
                    log_error "This IAM role is not registered as a service account."
                    log_error "Contact your org administrator to register this service account."
                    ;;
                *)
                    log_error "Access denied: $error_msg"
                    ;;
            esac
            exit 2
            ;;
        429)
            # Rate limited
            log_error "Rate limited by gateway. Please wait and try again."
            exit 2
            ;;
        500|502|503|504)
            # Server error
            log_error "Gateway server error (HTTP $http_code). Please try again later."
            exit 4
            ;;
        000)
            # curl failed to connect
            log_error "Failed to connect to gateway at $GATEWAY_URL"
            log_error "Check your network connection and gateway URL."
            exit 4
            ;;
        *)
            # Unknown error
            log_error "Unexpected response from gateway (HTTP $http_code)"
            log_debug "Response: $response"
            exit 2
            ;;
    esac
}

# Main function
main() {
    parse_args "$@"
    check_dependencies
    validate_config

    log_debug "$SCRIPT_NAME v$VERSION starting..."

    # Get AWS credentials
    get_aws_credentials

    # Exchange for gateway token
    exchange_credentials "$AWS_CREDS_ACCESS_KEY" "$AWS_CREDS_SECRET_KEY" "$AWS_CREDS_SESSION_TOKEN"
}

# Run main if not being sourced (for testing)
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
