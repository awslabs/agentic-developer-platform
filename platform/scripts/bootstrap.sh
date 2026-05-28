#!/bin/bash
#
# Bootstrap ADP Platform
# Creates Terraform state backend and deploys base infrastructure
#

set -e

# Load deployment config — populates ADP_ACCOUNT_ID, ADP_REGION,
# ADP_ENVIRONMENT, ADP_STATE_BUCKET, etc. Falls back to runtime defaults
# when config/deployment.yml is absent (preserves pre-config behavior).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=load-deploy-config.sh
source "${SCRIPT_DIR}/load-deploy-config.sh"

# Map config-helper outputs onto this script's local variable names.
AWS_REGION="$ADP_REGION"
ENVIRONMENT="$ADP_ENVIRONMENT"
ACCOUNT_ID="$ADP_ACCOUNT_ID"
BUCKET_NAME="$ADP_STATE_BUCKET"

echo "Bootstrapping ADP Platform..."
echo "Region: $AWS_REGION"
echo "Environment: $ENVIRONMENT"
echo "AWS Account: $ACCOUNT_ID"

# Create Terraform state bucket
echo "Creating S3 bucket: $BUCKET_NAME"

if ! aws s3api head-bucket --bucket "$BUCKET_NAME" 2>/dev/null; then
    aws s3api create-bucket --bucket "$BUCKET_NAME" --region "$AWS_REGION"
    aws s3api put-bucket-versioning --bucket "$BUCKET_NAME" --versioning-configuration Status=Enabled
    aws s3api put-bucket-encryption --bucket "$BUCKET_NAME" \
        --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
    aws s3api put-public-access-block --bucket "$BUCKET_NAME" \
        --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
    echo "S3 bucket created"
else
    echo "S3 bucket already exists"
fi

# Create DynamoDB table for state locking
TABLE_NAME="adp-terraform-locks"
echo "Creating DynamoDB table: $TABLE_NAME"

if ! aws dynamodb describe-table --table-name "$TABLE_NAME" --region "$AWS_REGION" 2>/dev/null; then
    aws dynamodb create-table --table-name "$TABLE_NAME" \
        --attribute-definitions AttributeName=LockID,AttributeType=S \
        --key-schema AttributeName=LockID,KeyType=HASH \
        --billing-mode PAY_PER_REQUEST --region "$AWS_REGION"
    aws dynamodb wait table-exists --table-name "$TABLE_NAME" --region "$AWS_REGION"
    echo "DynamoDB table created"
else
    echo "DynamoDB table already exists"
fi

# Update backend config with account ID
# Rewrites both the legacy ACCOUNT_ID placeholder AND any prior account id
# baked into adp-terraform-state-* bucket names. This makes bootstrap.sh
# safe to re-run in any account: pulls in the value from `aws sts get-
# caller-identity` and rewrites tfvars so subsequent `terraform init` lands
# on the right state bucket.
echo "Updating backend configuration..."
find environments/ -name "*.tfvars" -exec sed -i \
    -e "s/ACCOUNT_ID/${ACCOUNT_ID}/g" \
    -e "s/adp-terraform-state-[0-9]\{12\}/adp-terraform-state-${ACCOUNT_ID}/g" \
    {} \;

echo ""
echo "Bootstrap complete!"
echo ""
echo "Next steps:"
echo "  1. cd platform/infra"
echo "  2. terraform init -backend-config=../../environments/${ENVIRONMENT}/backend.tfvars"
echo "  3. terraform apply -var-file=../../environments/${ENVIRONMENT}/platform.tfvars"
