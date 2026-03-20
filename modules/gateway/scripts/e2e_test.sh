#!/bin/bash
# =============================================================================
# E2E Test Script for Bedrock Gateway
# =============================================================================
# Issue #127: Validates M2M agent flow, Human CLI flow, and SSE streaming
# through the gateway with actual Bedrock model calls.
#
# Prerequisites:
#   - AWS CLI configured with access to Secrets Manager and Cognito
#   - jq installed for JSON parsing
#   - curl installed for HTTP requests
#
# Usage:
#   ./scripts/e2e_test.sh [--cloudfront-url URL] [--user-pool-id ID]
#
# Environment Variables:
#   CLOUDFRONT_URL     - CloudFront distribution URL (default: auto-detect)
#   USER_POOL_ID       - Cognito User Pool ID (default: auto-detect)
#   TEST_USER_EMAIL    - Test user email (default: pranavsharma1000@gmail.com)
#   TEST_USER_PASSWORD - Test user password (must be set for human flow test)
#   SECRET_ID          - Secrets Manager secret ID for agent credentials
#   SKIP_M2M_TEST      - Set to "true" to skip M2M test
#   SKIP_HUMAN_TEST    - Set to "true" to skip human CLI test
#   SKIP_STREAMING_TEST - Set to "true" to skip streaming test
# =============================================================================

set -euo pipefail

# ANSI color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Counters
TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

# =============================================================================
# Utility Functions
# =============================================================================

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[PASS]${NC} $1"
}

log_fail() {
    echo -e "${RED}[FAIL]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_section() {
    echo ""
    echo "============================================================================="
    echo -e "${BLUE}$1${NC}"
    echo "============================================================================="
}

checkpoint() {
    local name="$1"
    local result="$2"
    local message="${3:-}"

    TESTS_RUN=$((TESTS_RUN + 1))

    if [ "$result" = "PASS" ]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        log_success "$name"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        log_fail "$name"
        if [ -n "$message" ]; then
            echo "       Error: $message"
        fi
    fi
}

# =============================================================================
# Configuration
# =============================================================================

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --cloudfront-url)
            CLOUDFRONT_URL="$2"
            shift 2
            ;;
        --user-pool-id)
            USER_POOL_ID="$2"
            shift 2
            ;;
        --secret-id)
            SECRET_ID="$2"
            shift 2
            ;;
        --help)
            echo "Usage: $0 [--cloudfront-url URL] [--user-pool-id ID] [--secret-id SECRET]"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Default configuration
AWS_REGION="${AWS_REGION:-us-east-1}"
ENVIRONMENT="${ENVIRONMENT:-dev}"
SECRET_ID="${SECRET_ID:-bedrockgw-${ENVIRONMENT}-agent-cognito-credentials}"
TEST_USER_EMAIL="${TEST_USER_EMAIL:-pranavsharma1000@gmail.com}"
TEST_USER_PASSWORD="${TEST_USER_PASSWORD:-}"

# Auto-detect CloudFront URL if not provided
if [ -z "${CLOUDFRONT_URL:-}" ]; then
    log_info "Auto-detecting CloudFront URL..."

    # First try: Find by Comment containing "bedrockgw-{env}"
    CLOUDFRONT_URL=$(aws cloudfront list-distributions \
        --query "DistributionList.Items[?contains(Comment, 'bedrockgw-${ENVIRONMENT}')].DomainName | [0]" \
        --output text 2>/dev/null) || CLOUDFRONT_URL=""

    # Second try: Find by Origin containing "bedrockgw-{env}" or "k8s-bedrockg"
    if [ -z "$CLOUDFRONT_URL" ] || [ "$CLOUDFRONT_URL" = "None" ]; then
        CLOUDFRONT_URL=$(aws cloudfront list-distributions \
            --query "DistributionList.Items[?contains(Origins.Items[0].DomainName, 'k8s-bedrockg')].DomainName | [0]" \
            --output text 2>/dev/null) || CLOUDFRONT_URL=""
    fi

    # Third try: Find by Origin containing ALB internal prefix
    if [ -z "$CLOUDFRONT_URL" ] || [ "$CLOUDFRONT_URL" = "None" ]; then
        CLOUDFRONT_URL=$(aws cloudfront list-distributions \
            --query "DistributionList.Items[?contains(Origins.Items[0].DomainName, 'internal-k8s')].DomainName | [0]" \
            --output text 2>/dev/null) || CLOUDFRONT_URL=""
    fi

    if [ -z "$CLOUDFRONT_URL" ] || [ "$CLOUDFRONT_URL" = "None" ]; then
        log_fail "Could not auto-detect CloudFront URL. Please provide --cloudfront-url"
        exit 1
    fi

    # Ensure HTTPS prefix
    if [[ ! "$CLOUDFRONT_URL" =~ ^https:// ]]; then
        CLOUDFRONT_URL="https://${CLOUDFRONT_URL}"
    fi
fi

# Auto-detect User Pool ID if not provided
if [ -z "${USER_POOL_ID:-}" ]; then
    log_info "Auto-detecting Cognito User Pool ID..."
    USER_POOL_ID=$(aws cognito-idp list-user-pools --max-results 50 \
        --query "UserPools[?contains(Name, 'bedrockgw-${ENVIRONMENT}')].Id | [0]" \
        --output text 2>/dev/null) || USER_POOL_ID=""

    if [ -z "$USER_POOL_ID" ] || [ "$USER_POOL_ID" = "None" ]; then
        log_warn "Could not auto-detect User Pool ID. Human CLI flow test may be skipped."
    fi
fi

log_section "E2E Test Configuration"
log_info "CloudFront URL: ${CLOUDFRONT_URL}"
log_info "AWS Region: ${AWS_REGION}"
log_info "Environment: ${ENVIRONMENT}"
log_info "Secret ID: ${SECRET_ID}"
log_info "User Pool ID: ${USER_POOL_ID:-Not configured}"
log_info "Test User: ${TEST_USER_EMAIL}"

# =============================================================================
# Test 1: M2M Agent Flow (client_credentials)
# =============================================================================

run_m2m_test() {
    log_section "Test 1: M2M Agent Flow (client_credentials)"

    if [ "${SKIP_M2M_TEST:-false}" = "true" ]; then
        log_warn "Skipping M2M test (SKIP_M2M_TEST=true)"
        return 0
    fi

    # Step 1: Get agent credentials from Secrets Manager
    log_info "Step 1: Retrieving agent credentials from Secrets Manager..."

    CREDS=$(aws secretsmanager get-secret-value \
        --secret-id "$SECRET_ID" \
        --query SecretString --output text 2>&1) || {
        checkpoint "M2M: Retrieve credentials from Secrets Manager" "FAIL" "$CREDS"
        return 1
    }

    CLIENT_ID=$(echo "$CREDS" | jq -r '.client_id')
    CLIENT_SECRET=$(echo "$CREDS" | jq -r '.client_secret')
    TOKEN_ENDPOINT=$(echo "$CREDS" | jq -r '.token_endpoint')
    SCOPE=$(echo "$CREDS" | jq -r '.scope')

    if [ -z "$CLIENT_ID" ] || [ "$CLIENT_ID" = "null" ]; then
        checkpoint "M2M: Parse client_id from secret" "FAIL" "client_id is null or empty"
        return 1
    fi

    checkpoint "M2M: Retrieve credentials from Secrets Manager" "PASS"
    log_info "Client ID: ${CLIENT_ID:0:10}..."
    log_info "Token Endpoint: $TOKEN_ENDPOINT"
    log_info "Scope: $SCOPE"

    # Step 2: Get JWT token via client_credentials grant
    log_info "Step 2: Obtaining JWT token via client_credentials..."

    TOKEN_RESPONSE=$(curl -s -X POST "$TOKEN_ENDPOINT" \
        -H "Content-Type: application/x-www-form-urlencoded" \
        -d "grant_type=client_credentials&client_id=$CLIENT_ID&client_secret=$CLIENT_SECRET&scope=$SCOPE" 2>&1) || {
        checkpoint "M2M: Obtain JWT token" "FAIL" "curl failed: $TOKEN_RESPONSE"
        return 1
    }

    ACCESS_TOKEN=$(echo "$TOKEN_RESPONSE" | jq -r '.access_token')

    if [ -z "$ACCESS_TOKEN" ] || [ "$ACCESS_TOKEN" = "null" ]; then
        ERROR=$(echo "$TOKEN_RESPONSE" | jq -r '.error // .message // "Unknown error"')
        checkpoint "M2M: Obtain JWT token" "FAIL" "Token is null. Error: $ERROR"
        echo "       Token response: $TOKEN_RESPONSE"
        return 1
    fi

    checkpoint "M2M: Obtain JWT token" "PASS"
    log_info "Token obtained (length: ${#ACCESS_TOKEN})"

    # Step 3: Health check with M2M token
    log_info "Step 3: Performing health check with M2M token..."

    HEALTH_RESPONSE=$(curl -s -w "\n%{http_code}" \
        -H "Authorization: Bearer $ACCESS_TOKEN" \
        "${CLOUDFRONT_URL}/api/health" 2>&1)

    HTTP_CODE=$(echo "$HEALTH_RESPONSE" | tail -n1)
    HEALTH_BODY=$(echo "$HEALTH_RESPONSE" | sed '$d')

    if [ "$HTTP_CODE" = "200" ]; then
        checkpoint "M2M: Health check" "PASS"
        log_info "Health response: $HEALTH_BODY"
    else
        checkpoint "M2M: Health check" "FAIL" "HTTP $HTTP_CODE - $HEALTH_BODY"
        return 1
    fi

    # Step 4: Bedrock call with M2M token
    log_info "Step 4: Calling Bedrock via gateway (non-streaming)..."

    BEDROCK_REQUEST='{
        "model": "claude-3-haiku",
        "messages": [{"role": "user", "content": "Say hello in exactly 3 words."}],
        "max_tokens": 50
    }'

    BEDROCK_RESPONSE=$(curl -s -w "\n%{http_code}" \
        -X POST "${CLOUDFRONT_URL}/api/v1/chat/completions" \
        -H "Authorization: Bearer $ACCESS_TOKEN" \
        -H "Content-Type: application/json" \
        -d "$BEDROCK_REQUEST" \
        --max-time 60 2>&1)

    HTTP_CODE=$(echo "$BEDROCK_RESPONSE" | tail -n1)
    BEDROCK_BODY=$(echo "$BEDROCK_RESPONSE" | sed '$d')

    if [ "$HTTP_CODE" = "200" ]; then
        # Verify response contains expected fields
        if echo "$BEDROCK_BODY" | jq -e '.choices[0].message.content' > /dev/null 2>&1; then
            CONTENT=$(echo "$BEDROCK_BODY" | jq -r '.choices[0].message.content')
            checkpoint "M2M: Bedrock call (non-streaming)" "PASS"
            log_info "Bedrock response: $CONTENT"
        else
            checkpoint "M2M: Bedrock call (non-streaming)" "FAIL" "Response missing expected fields"
            echo "       Response: $BEDROCK_BODY"
            return 1
        fi
    else
        checkpoint "M2M: Bedrock call (non-streaming)" "FAIL" "HTTP $HTTP_CODE - $BEDROCK_BODY"
        return 1
    fi

    # Store token for streaming test
    M2M_ACCESS_TOKEN="$ACCESS_TOKEN"
    export M2M_ACCESS_TOKEN

    log_success "M2M Agent Flow: All checkpoints passed!"
    return 0
}

# =============================================================================
# Test 2: Human CLI Flow (Cognito user auth)
# =============================================================================

run_human_test() {
    log_section "Test 2: Human CLI Flow (Cognito user auth)"

    if [ "${SKIP_HUMAN_TEST:-false}" = "true" ]; then
        log_warn "Skipping human CLI test (SKIP_HUMAN_TEST=true)"
        return 0
    fi

    if [ -z "$USER_POOL_ID" ] || [ "$USER_POOL_ID" = "None" ]; then
        log_warn "Skipping human CLI test (User Pool ID not configured)"
        return 0
    fi

    if [ -z "$TEST_USER_PASSWORD" ]; then
        log_warn "Skipping human CLI test (TEST_USER_PASSWORD not set)"
        log_info "Set TEST_USER_PASSWORD environment variable to enable this test"
        return 0
    fi

    # Step 1: Get the CLI client ID (public client without secret)
    log_info "Step 1: Getting Cognito App Client ID..."

    # Get the client that contains 'client' in name (the public PKCE client)
    CLIENT_ID=$(aws cognito-idp list-user-pool-clients \
        --user-pool-id "$USER_POOL_ID" \
        --query "UserPoolClients[?contains(ClientName, 'client') && !contains(ClientName, 'agent')].ClientId | [0]" \
        --output text 2>/dev/null) || CLIENT_ID=""

    if [ -z "$CLIENT_ID" ] || [ "$CLIENT_ID" = "None" ]; then
        # Fallback to first client
        CLIENT_ID=$(aws cognito-idp list-user-pool-clients \
            --user-pool-id "$USER_POOL_ID" \
            --query "UserPoolClients[0].ClientId" \
            --output text 2>/dev/null) || CLIENT_ID=""
    fi

    if [ -z "$CLIENT_ID" ] || [ "$CLIENT_ID" = "None" ]; then
        checkpoint "Human: Get App Client ID" "FAIL" "Could not find Cognito App Client"
        return 1
    fi

    checkpoint "Human: Get App Client ID" "PASS"
    log_info "Client ID: $CLIENT_ID"

    # Step 2: Authenticate as test user using ADMIN_USER_PASSWORD_AUTH
    log_info "Step 2: Authenticating as test user..."

    AUTH_RESULT=$(aws cognito-idp admin-initiate-auth \
        --user-pool-id "$USER_POOL_ID" \
        --client-id "$CLIENT_ID" \
        --auth-flow ADMIN_USER_PASSWORD_AUTH \
        --auth-parameters "USERNAME=$TEST_USER_EMAIL,PASSWORD=$TEST_USER_PASSWORD" \
        2>&1) || {
        # Try USER_PASSWORD_AUTH as fallback
        log_warn "ADMIN_USER_PASSWORD_AUTH failed, trying USER_PASSWORD_AUTH..."
        AUTH_RESULT=$(aws cognito-idp initiate-auth \
            --client-id "$CLIENT_ID" \
            --auth-flow USER_PASSWORD_AUTH \
            --auth-parameters "USERNAME=$TEST_USER_EMAIL,PASSWORD=$TEST_USER_PASSWORD" \
            2>&1) || {
            checkpoint "Human: Authenticate user" "FAIL" "$AUTH_RESULT"
            return 1
        }
    }

    ACCESS_TOKEN=$(echo "$AUTH_RESULT" | jq -r '.AuthenticationResult.AccessToken')

    if [ -z "$ACCESS_TOKEN" ] || [ "$ACCESS_TOKEN" = "null" ]; then
        # Check for challenge response (e.g., NEW_PASSWORD_REQUIRED)
        CHALLENGE=$(echo "$AUTH_RESULT" | jq -r '.ChallengeName // empty')
        if [ -n "$CHALLENGE" ]; then
            checkpoint "Human: Authenticate user" "FAIL" "Authentication challenge required: $CHALLENGE"
        else
            checkpoint "Human: Authenticate user" "FAIL" "Access token is null"
        fi
        echo "       Auth result: $AUTH_RESULT"
        return 1
    fi

    checkpoint "Human: Authenticate user" "PASS"
    log_info "Token obtained (length: ${#ACCESS_TOKEN})"

    # Step 3: Health check with human token
    log_info "Step 3: Performing health check with human token..."

    HEALTH_RESPONSE=$(curl -s -w "\n%{http_code}" \
        -H "Authorization: Bearer $ACCESS_TOKEN" \
        "${CLOUDFRONT_URL}/api/health" 2>&1)

    HTTP_CODE=$(echo "$HEALTH_RESPONSE" | tail -n1)
    HEALTH_BODY=$(echo "$HEALTH_RESPONSE" | sed '$d')

    if [ "$HTTP_CODE" = "200" ]; then
        checkpoint "Human: Health check" "PASS"
        log_info "Health response: $HEALTH_BODY"
    else
        checkpoint "Human: Health check" "FAIL" "HTTP $HTTP_CODE - $HEALTH_BODY"
        return 1
    fi

    # Step 4: Bedrock call with human token
    log_info "Step 4: Calling Bedrock via gateway (non-streaming)..."

    BEDROCK_REQUEST='{
        "model": "claude-3-haiku",
        "messages": [{"role": "user", "content": "What is 2+2? Answer with just the number."}],
        "max_tokens": 50
    }'

    BEDROCK_RESPONSE=$(curl -s -w "\n%{http_code}" \
        -X POST "${CLOUDFRONT_URL}/api/v1/chat/completions" \
        -H "Authorization: Bearer $ACCESS_TOKEN" \
        -H "Content-Type: application/json" \
        -d "$BEDROCK_REQUEST" \
        --max-time 60 2>&1)

    HTTP_CODE=$(echo "$BEDROCK_RESPONSE" | tail -n1)
    BEDROCK_BODY=$(echo "$BEDROCK_RESPONSE" | sed '$d')

    if [ "$HTTP_CODE" = "200" ]; then
        if echo "$BEDROCK_BODY" | jq -e '.choices[0].message.content' > /dev/null 2>&1; then
            CONTENT=$(echo "$BEDROCK_BODY" | jq -r '.choices[0].message.content')
            checkpoint "Human: Bedrock call (non-streaming)" "PASS"
            log_info "Bedrock response: $CONTENT"
        else
            checkpoint "Human: Bedrock call (non-streaming)" "FAIL" "Response missing expected fields"
            echo "       Response: $BEDROCK_BODY"
            return 1
        fi
    else
        checkpoint "Human: Bedrock call (non-streaming)" "FAIL" "HTTP $HTTP_CODE - $BEDROCK_BODY"
        return 1
    fi

    # Step 5: Test admin endpoint (if user has admin access)
    log_info "Step 5: Testing admin endpoint..."

    ADMIN_RESPONSE=$(curl -s -w "\n%{http_code}" \
        -H "Authorization: Bearer $ACCESS_TOKEN" \
        "${CLOUDFRONT_URL}/api/admin/organizations" 2>&1)

    HTTP_CODE=$(echo "$ADMIN_RESPONSE" | tail -n1)
    ADMIN_BODY=$(echo "$ADMIN_RESPONSE" | sed '$d')

    if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "403" ]; then
        # 200 = admin access granted, 403 = not admin (both are valid)
        checkpoint "Human: Admin endpoint accessible" "PASS"
        if [ "$HTTP_CODE" = "200" ]; then
            log_info "Admin access granted"
        else
            log_info "Admin access denied (expected for non-admin users)"
        fi
    else
        checkpoint "Human: Admin endpoint accessible" "FAIL" "HTTP $HTTP_CODE - $ADMIN_BODY"
        return 1
    fi

    # Store token for streaming test
    HUMAN_ACCESS_TOKEN="$ACCESS_TOKEN"
    export HUMAN_ACCESS_TOKEN

    log_success "Human CLI Flow: All checkpoints passed!"
    return 0
}

# =============================================================================
# Test 3: Streaming Response (SSE)
# =============================================================================

run_streaming_test() {
    log_section "Test 3: Streaming Response (SSE)"

    if [ "${SKIP_STREAMING_TEST:-false}" = "true" ]; then
        log_warn "Skipping streaming test (SKIP_STREAMING_TEST=true)"
        return 0
    fi

    # Use M2M token if available, otherwise try human token
    ACCESS_TOKEN="${M2M_ACCESS_TOKEN:-${HUMAN_ACCESS_TOKEN:-}}"

    if [ -z "$ACCESS_TOKEN" ]; then
        log_warn "Skipping streaming test (no access token available)"
        log_info "Run M2M or Human test first to obtain an access token"
        return 0
    fi

    log_info "Testing SSE streaming through CloudFront..."

    STREAM_REQUEST='{
        "model": "claude-3-haiku",
        "messages": [{"role": "user", "content": "Count from 1 to 5, one number per line."}],
        "max_tokens": 100,
        "stream": true
    }'

    # Use -N for no buffering to see SSE stream
    STREAM_OUTPUT=$(timeout 60 curl -N -s \
        -X POST "${CLOUDFRONT_URL}/api/v1/chat/completions" \
        -H "Authorization: Bearer $ACCESS_TOKEN" \
        -H "Content-Type: application/json" \
        -d "$STREAM_REQUEST" 2>&1) || {
        log_warn "Stream request timed out or failed"
    }

    # Check for SSE format (data: {...} lines)
    if echo "$STREAM_OUTPUT" | grep -q "^data:"; then
        checkpoint "Streaming: SSE format received" "PASS"

        # Count data lines
        DATA_LINES=$(echo "$STREAM_OUTPUT" | grep -c "^data:" || echo "0")
        log_info "Received $DATA_LINES SSE data lines"

        # Check for [DONE] terminator
        if echo "$STREAM_OUTPUT" | grep -q "data: \[DONE\]"; then
            checkpoint "Streaming: [DONE] terminator received" "PASS"
        else
            checkpoint "Streaming: [DONE] terminator received" "FAIL" "No [DONE] terminator found"
        fi

        # Show first few lines of output
        log_info "First 5 SSE lines:"
        echo "$STREAM_OUTPUT" | head -5

    else
        checkpoint "Streaming: SSE format received" "FAIL" "No 'data:' lines in response"
        log_info "Response preview:"
        echo "$STREAM_OUTPUT" | head -10
        return 1
    fi

    log_success "Streaming Response: All checkpoints passed!"
    return 0
}

# =============================================================================
# Main Execution
# =============================================================================

main() {
    log_section "Bedrock Gateway E2E Tests"
    log_info "Starting E2E tests..."

    # Run tests
    M2M_RESULT=0
    HUMAN_RESULT=0
    STREAMING_RESULT=0

    run_m2m_test || M2M_RESULT=$?
    run_human_test || HUMAN_RESULT=$?
    run_streaming_test || STREAMING_RESULT=$?

    # Summary
    log_section "Test Summary"
    echo ""
    echo "Tests Run:    $TESTS_RUN"
    echo -e "Tests Passed: ${GREEN}$TESTS_PASSED${NC}"
    echo -e "Tests Failed: ${RED}$TESTS_FAILED${NC}"
    echo ""

    if [ $TESTS_FAILED -eq 0 ]; then
        log_success "All E2E tests passed!"
        exit 0
    else
        log_fail "$TESTS_FAILED test(s) failed"
        exit 1
    fi
}

# Run main
main "$@"
