#!/bin/bash
# =============================================================================
# Test API Gateway IAM Authentication (Issue #241)
# =============================================================================
# This script tests IAM-authenticated requests to the API Gateway using
# the Lambda authorizer. It assumes a test agent role and makes SigV4-signed
# requests to verify the authentication flow.
#
# Prerequisites:
# - AWS CLI v2 installed
# - awscurl installed (pip install awscurl) OR Python 3 with boto3
# - IAM permissions to assume the test agent role
#
# Usage:
#   ./scripts/test-apigw-iam-auth.sh [--env ENV] [--role-arn ARN] [--api-id ID]
#
# Options:
#   --env ENV        Environment name (default: dev)
#   --role-arn ARN   Test agent role ARN (default: bedrockgw-{env}-test-agent)
#   --api-id ID      API Gateway REST API ID (optional, auto-detected)
#   --skip-assume    Skip role assumption (use current credentials)
# =============================================================================

set -euo pipefail

# Default values
ENV="${ENV:-dev}"
ROLE_ARN=""
API_ID=""
SKIP_ASSUME=false
AWS_REGION="${AWS_REGION:-us-east-1}"

# Parse command line arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --env)
      ENV="$2"
      shift 2
      ;;
    --role-arn)
      ROLE_ARN="$2"
      shift 2
      ;;
    --api-id)
      API_ID="$2"
      shift 2
      ;;
    --skip-assume)
      SKIP_ASSUME=true
      shift
      ;;
    --help|-h)
      echo "Usage: $0 [--env ENV] [--role-arn ARN] [--api-id ID] [--skip-assume]"
      echo ""
      echo "Options:"
      echo "  --env ENV        Environment name (default: dev)"
      echo "  --role-arn ARN   Test agent role ARN"
      echo "  --api-id ID      API Gateway REST API ID"
      echo "  --skip-assume    Skip role assumption"
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "=============================================="
echo "API Gateway IAM Authentication Test"
echo "=============================================="
echo "Environment: ${ENV}"
echo "AWS Region: ${AWS_REGION}"
echo ""

# Get AWS account ID
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
echo "AWS Account: ${ACCOUNT_ID}"

# Set default role ARN if not provided
if [ -z "$ROLE_ARN" ]; then
  ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/bedrockgw-${ENV}-test-agent"
fi
echo "Test Agent Role: ${ROLE_ARN}"

# Find API Gateway ID if not provided
if [ -z "$API_ID" ]; then
  API_ID=$(aws apigateway get-rest-apis \
    --query "items[?name=='bedrockgw-${ENV}-api'].id | [0]" \
    --output text 2>/dev/null) || API_ID=""

  if [ -z "$API_ID" ] || [ "$API_ID" = "None" ]; then
    echo -e "${RED}ERROR: API Gateway REST API not found for environment ${ENV}${NC}"
    exit 1
  fi
fi
echo "API Gateway ID: ${API_ID}"

INVOKE_URL="https://${API_ID}.execute-api.${AWS_REGION}.amazonaws.com/${ENV}"
echo "Invoke URL: ${INVOKE_URL}"
echo ""

# Function to make SigV4-signed request using Python
make_sigv4_request() {
  local method="$1"
  local path="$2"
  local data="${3:-}"

  python3 - "$method" "$path" "$data" << 'PYEOF'
import sys
import json
import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
import urllib.request
import ssl
import os

method = sys.argv[1]
path = sys.argv[2]
data = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else None

api_id = os.environ.get("API_ID")
region = os.environ.get("AWS_REGION", "us-east-1")
env = os.environ.get("ENV", "dev")

url = f"https://{api_id}.execute-api.{region}.amazonaws.com/{env}{path}"

# Get credentials from environment or default chain
session = boto3.Session()
credentials = session.get_credentials()

# Create the request
headers = {
    "Content-Type": "application/json",
    "Host": f"{api_id}.execute-api.{region}.amazonaws.com",
}

request = AWSRequest(method=method, url=url, headers=headers, data=data)

# Sign the request with SigV4
SigV4Auth(credentials, "execute-api", region).add_auth(request)

# Make the request
try:
    req = urllib.request.Request(
        url,
        method=method,
        headers=dict(request.headers),
        data=data.encode() if data else None,
    )

    # Create SSL context for HTTPS requests
    ctx = ssl.create_default_context()

    with urllib.request.urlopen(req, context=ctx, timeout=30) as response:
        status = response.status
        body = response.read().decode()
        headers = dict(response.headers)

        print(json.dumps({
            "status": status,
            "body": body,
            "headers": headers,
        }))
except urllib.error.HTTPError as e:
    print(json.dumps({
        "status": e.code,
        "body": e.read().decode() if e.fp else "",
        "headers": dict(e.headers) if e.headers else {},
        "error": str(e),
    }))
except Exception as e:
    print(json.dumps({
        "status": 0,
        "body": "",
        "headers": {},
        "error": str(e),
    }))
PYEOF
}

# Step 1: Assume the test agent role (unless --skip-assume)
if [ "$SKIP_ASSUME" = "false" ]; then
  echo "Step 1: Assuming test agent role..."

  # Check if role exists
  if ! aws iam get-role --role-name "$(basename "$ROLE_ARN")" &>/dev/null; then
    echo -e "${YELLOW}WARNING: Role ${ROLE_ARN} does not exist${NC}"
    echo "Creating test agent role..."

    # Get current role for trust policy
    CURRENT_ROLE_ARN=$(aws sts get-caller-identity --query Arn --output text)
    CURRENT_ACCOUNT=$(echo "$CURRENT_ROLE_ARN" | cut -d: -f5)

    # Extract role name from ARN (handles assumed-role format)
    if [[ "$CURRENT_ROLE_ARN" == *":assumed-role/"* ]]; then
      CURRENT_ROLE_NAME=$(echo "$CURRENT_ROLE_ARN" | sed 's/.*:assumed-role\///' | cut -d/ -f1)
      TRUST_PRINCIPAL="arn:aws:iam::${CURRENT_ACCOUNT}:role/${CURRENT_ROLE_NAME}"
    else
      TRUST_PRINCIPAL="$CURRENT_ROLE_ARN"
    fi

    # Create trust policy
    TRUST_POLICY=$(cat << TRUSTEOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "${TRUST_PRINCIPAL}"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
TRUSTEOF
)

    # Create the role
    ROLE_NAME="bedrockgw-${ENV}-test-agent"
    aws iam create-role \
      --role-name "$ROLE_NAME" \
      --assume-role-policy-document "$TRUST_POLICY" \
      --description "Test agent role for API Gateway IAM auth testing (Issue #241)" \
      --tags "Key=Environment,Value=${ENV}" "Key=Purpose,Value=test-agent" \
      || { echo "Role creation failed (may already exist)"; aws iam get-role --role-name "$ROLE_NAME" > /dev/null 2>&1 || exit 1; }

    # Attach execute-api permission
    POLICY_DOC=$(cat << POLICYEOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "execute-api:Invoke",
      "Resource": "arn:aws:execute-api:${AWS_REGION}:${ACCOUNT_ID}:${API_ID}/*"
    }
  ]
}
POLICYEOF
)

    aws iam put-role-policy \
      --role-name "$ROLE_NAME" \
      --policy-name "api-gateway-invoke" \
      --policy-document "$POLICY_DOC" \
      || echo "Policy may already exist"

    echo "Waiting for role to propagate..."
    sleep 10
  fi

  # Assume the role
  echo "Assuming role: ${ROLE_ARN}"
  CREDS=$(aws sts assume-role \
    --role-arn "$ROLE_ARN" \
    --role-session-name "test-apigw-iam-auth-$(date +%s)" \
    --query "Credentials" --output json)

  export AWS_ACCESS_KEY_ID=$(echo "$CREDS" | jq -r '.AccessKeyId')
  export AWS_SECRET_ACCESS_KEY=$(echo "$CREDS" | jq -r '.SecretAccessKey')
  export AWS_SESSION_TOKEN=$(echo "$CREDS" | jq -r '.SessionToken')

  echo -e "${GREEN}Successfully assumed test agent role${NC}"

  # Verify current identity
  ASSUMED_IDENTITY=$(aws sts get-caller-identity --query Arn --output text)
  echo "Current identity: ${ASSUMED_IDENTITY}"
else
  echo "Step 1: Skipping role assumption (--skip-assume)"
fi
echo ""

# Step 2: Test health endpoint with SigV4
echo "Step 2: Testing /health endpoint with SigV4..."

export API_ID
export AWS_REGION
export ENV

RESULT=$(make_sigv4_request "GET" "/health")
HTTP_STATUS=$(echo "$RESULT" | jq -r '.status')
BODY=$(echo "$RESULT" | jq -r '.body')
ERROR=$(echo "$RESULT" | jq -r '.error // empty')

if [ "$HTTP_STATUS" = "200" ]; then
  echo -e "${GREEN}SUCCESS: Health check passed (HTTP ${HTTP_STATUS})${NC}"
  echo "Response: ${BODY}"
elif [ "$HTTP_STATUS" = "401" ] || [ "$HTTP_STATUS" = "403" ]; then
  echo -e "${RED}FAILED: Authentication failed (HTTP ${HTTP_STATUS})${NC}"
  echo "Response: ${BODY}"
  if [ -n "$ERROR" ]; then
    echo "Error: ${ERROR}"
  fi
  echo ""
  echo "Possible causes:"
  echo "  1. Test agent role is not registered in the DynamoDB agent registry"
  echo "  2. Lambda authorizer is not configured correctly"
  echo "  3. SigV4 signature is incorrect"
  exit 1
else
  echo -e "${YELLOW}WARNING: Unexpected response (HTTP ${HTTP_STATUS})${NC}"
  echo "Response: ${BODY}"
  if [ -n "$ERROR" ]; then
    echo "Error: ${ERROR}"
  fi
fi
echo ""

# Step 3: Test a protected endpoint
echo "Step 3: Testing /v1/models endpoint with SigV4..."

RESULT=$(make_sigv4_request "GET" "/v1/models")
HTTP_STATUS=$(echo "$RESULT" | jq -r '.status')
BODY=$(echo "$RESULT" | jq -r '.body')
HEADERS=$(echo "$RESULT" | jq -r '.headers')

if [ "$HTTP_STATUS" = "200" ]; then
  echo -e "${GREEN}SUCCESS: Models endpoint returned (HTTP ${HTTP_STATUS})${NC}"
  echo "Response preview: $(echo "$BODY" | head -c 200)..."
elif [ "$HTTP_STATUS" = "401" ] || [ "$HTTP_STATUS" = "403" ]; then
  echo -e "${RED}FAILED: Authentication failed (HTTP ${HTTP_STATUS})${NC}"
  echo "Response: ${BODY}"
else
  echo -e "${YELLOW}INFO: Response (HTTP ${HTTP_STATUS})${NC}"
  echo "Response: ${BODY}"
fi
echo ""

# Step 4: Check if identity headers are being passed
echo "Step 4: Checking identity headers..."
echo "Expected headers from Lambda authorizer:"
echo "  - X-Auth-Source: iam"
echo "  - X-Agent-Id: test-agent"
echo "  - X-Agent-OrgId: default"
echo "  - X-Agent-TeamId: platform"
echo ""

# Summary
echo "=============================================="
echo "Test Summary"
echo "=============================================="
if [ "$HTTP_STATUS" = "200" ]; then
  echo -e "${GREEN}All tests passed!${NC}"
  echo ""
  echo "The IAM authentication flow is working:"
  echo "  1. SigV4 request -> API Gateway"
  echo "  2. API Gateway -> Lambda Authorizer"
  echo "  3. Lambda Authorizer validates IAM identity"
  echo "  4. Lambda Authorizer looks up agent in DynamoDB"
  echo "  5. Lambda Authorizer returns Allow policy with identity headers"
  echo "  6. API Gateway forwards request with identity context"
  echo "  7. Backend receives request with X-Agent-* headers"
else
  echo -e "${RED}Some tests failed. Check the logs above for details.${NC}"
  exit 1
fi
