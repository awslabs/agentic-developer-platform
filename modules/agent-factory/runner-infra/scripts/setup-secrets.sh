#!/bin/bash
set -euo pipefail

# Store GitHub PAT in AWS Secrets Manager

if [ $# -lt 1 ]; then
    echo "Usage: $0 <github-pat>"
    echo ""
    echo "Create a GitHub PAT with these scopes:"
    echo "  - repo (for repository-level runners)"
    echo "  - admin:org (for organization-level runners)"
    echo ""
    echo "Generate at: https://github.com/settings/tokens"
    exit 1
fi

PAT=$1
SECRET_NAME="github-arc-runner/pat"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# Get region from Terraform
cd "$ROOT_DIR/infrastructure"
if [ -f terraform.tfvars ]; then
    AWS_REGION=$(grep 'aws_region' terraform.tfvars | cut -d'"' -f2)
else
    AWS_REGION="us-east-1"
fi

echo "=========================================="
echo "Storing GitHub PAT in Secrets Manager"
echo "=========================================="
echo "Secret: $SECRET_NAME"
echo "Region: $AWS_REGION"
echo ""

# Check if secret exists
if aws secretsmanager describe-secret --secret-id "$SECRET_NAME" --region "$AWS_REGION" 2>/dev/null; then
    echo "Updating existing secret..."
    aws secretsmanager put-secret-value \
        --secret-id "$SECRET_NAME" \
        --secret-string "{\"token\": \"$PAT\"}" \
        --region "$AWS_REGION"
else
    echo "Creating new secret..."
    aws secretsmanager create-secret \
        --name "$SECRET_NAME" \
        --description "GitHub PAT for ARC runners" \
        --secret-string "{\"token\": \"$PAT\"}" \
        --region "$AWS_REGION"
fi

echo ""
echo "=========================================="
echo "PAT stored successfully!"
echo "=========================================="
