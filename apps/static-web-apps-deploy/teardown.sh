#!/bin/bash
# Teardown static web apps deployment
# Removes CloudFront distributions, OAC, S3 buckets
#
# Usage:
#   export AWS_DEFAULT_REGION=us-east-1
#   ./teardown.sh

set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-us-east-1}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
PREFIX="swa-examples"

echo "=== Static Web Apps Teardown ==="
echo "Account: $ACCOUNT_ID"
echo ""

APPS=("react-shopathome" "vite-microservices" "showcase")

# Step 1: Disable and delete CloudFront distributions
echo "=== Disabling CloudFront distributions ==="
for app in "${APPS[@]}"; do
  BUCKET="${PREFIX}-${app}-${ACCOUNT_ID}"
  ORIGIN_DOMAIN="${BUCKET}.s3.${REGION}.amazonaws.com"

  DIST_ID=$(aws cloudfront list-distributions \
    --query "DistributionList.Items[?Origins.Items[0].DomainName=='${ORIGIN_DOMAIN}'].Id" \
    --output text 2>/dev/null || echo "")

  if [ -z "$DIST_ID" ] || [ "$DIST_ID" = "None" ]; then
    echo "  $app: no distribution found"
    continue
  fi

  echo "  $app: disabling $DIST_ID..."

  # Get current config
  ETAG=$(aws cloudfront get-distribution-config --id "$DIST_ID" --query 'ETag' --output text)
  CONFIG=$(aws cloudfront get-distribution-config --id "$DIST_ID" --query 'DistributionConfig')

  # Disable the distribution
  DISABLED_CONFIG=$(echo "$CONFIG" | python3 -c "
import sys, json
c = json.load(sys.stdin)
c['Enabled'] = False
print(json.dumps(c))
")

  aws cloudfront update-distribution --id "$DIST_ID" --if-match "$ETAG" \
    --distribution-config "$DISABLED_CONFIG" >/dev/null

  echo "  $app: disabled. Waiting for deployment..."
  aws cloudfront wait distribution-deployed --id "$DIST_ID"

  # Delete
  ETAG=$(aws cloudfront get-distribution --id "$DIST_ID" --query 'ETag' --output text)
  aws cloudfront delete-distribution --id "$DIST_ID" --if-match "$ETAG"
  echo "  $app: deleted distribution"
done

# Step 2: Delete OAC
echo ""
echo "=== Deleting OAC ==="
OAC_NAME="${PREFIX}-oac"
OAC_ID=$(aws cloudfront list-origin-access-controls \
  --query "OriginAccessControlList.Items[?Name=='${OAC_NAME}'].Id" --output text 2>/dev/null || echo "")

if [ -n "$OAC_ID" ] && [ "$OAC_ID" != "None" ]; then
  ETAG=$(aws cloudfront get-origin-access-control --id "$OAC_ID" --query 'ETag' --output text)
  aws cloudfront delete-origin-access-control --id "$OAC_ID" --if-match "$ETAG"
  echo "  Deleted OAC: $OAC_ID"
else
  echo "  No OAC found"
fi

# Step 3: Empty and delete S3 buckets
echo ""
echo "=== Deleting S3 Buckets ==="
for app in "${APPS[@]}"; do
  BUCKET="${PREFIX}-${app}-${ACCOUNT_ID}"

  if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
    aws s3 rm "s3://${BUCKET}" --recursive --quiet
    aws s3api delete-bucket --bucket "$BUCKET"
    echo "  Deleted: $BUCKET"
  else
    echo "  Not found: $BUCKET"
  fi
done

echo ""
echo "=== Teardown Complete ==="
