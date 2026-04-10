#!/bin/bash
set -euo pipefail

# Add CloudWatch subscription filter to a log group
# Usage: ./add-subscription.sh <log-group-name> [repo-name] [filter-pattern]

if [ $# -lt 1 ]; then
    echo "Usage: $0 <log-group-name> [repo-name] [filter-pattern]"
    echo ""
    echo "Examples:"
    echo "  $0 /aws/lambda/my-app my-app"
    echo "  $0 /aws/lambda/my-app my-app '?ERROR ?Exception'"
    echo "  $0 /aws/lambda/my-app  # Uses tag for repo mapping"
    echo ""
    echo "If repo-name is provided, it will be set as a tag on the log group."
    echo "The Lambda function uses this tag to determine which repo to create issues in."
    exit 1
fi

LOG_GROUP=$1
REPO_NAME=${2:-""}
FILTER_PATTERN=${3:-"?ERROR ?Exception ?FATAL ?CRITICAL"}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TERRAFORM_DIR="$SCRIPT_DIR/../terraform"

# Get Lambda ARN from Terraform
cd "$TERRAFORM_DIR"
LAMBDA_ARN=$(terraform output -raw lambda_function_arn 2>/dev/null || echo "")

if [ -z "$LAMBDA_ARN" ]; then
    echo "ERROR: Could not get Lambda ARN. Make sure Terraform has been applied."
    exit 1
fi

echo "=========================================="
echo "Adding CloudWatch Subscription"
echo "=========================================="
echo "Log Group: $LOG_GROUP"
echo "Lambda ARN: $LAMBDA_ARN"
echo "Filter Pattern: $FILTER_PATTERN"
if [ -n "$REPO_NAME" ]; then
    echo "Repo: $REPO_NAME"
fi
echo ""

# Add repo tag if provided
if [ -n "$REPO_NAME" ]; then
    echo "Step 1: Tagging log group with repo..."
    
    # Get log group ARN
    AWS_REGION=$(aws configure get region || echo "us-east-1")
    AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
    LOG_GROUP_ARN="arn:aws:logs:${AWS_REGION}:${AWS_ACCOUNT}:log-group:${LOG_GROUP}"
    
    aws logs tag-resource \
        --resource-arn "$LOG_GROUP_ARN" \
        --tags "agent:repo=$REPO_NAME"
    
    echo "  Tagged with agent:repo=$REPO_NAME"
fi

# Create subscription filter
echo "Step 2: Creating subscription filter..."

FILTER_NAME="cloudwatch-agent-$(echo "$LOG_GROUP" | tr '/' '-' | sed 's/^-//')"

aws logs put-subscription-filter \
    --log-group-name "$LOG_GROUP" \
    --filter-name "$FILTER_NAME" \
    --filter-pattern "$FILTER_PATTERN" \
    --destination-arn "$LAMBDA_ARN"

echo ""
echo "=========================================="
echo "✅ Subscription added successfully!"
echo "=========================================="
echo ""
echo "When errors matching '$FILTER_PATTERN' appear in $LOG_GROUP,"
echo "a GitHub issue will be created in ${REPO_NAME:-'the configured repo'}."
echo ""
