#!/bin/bash
# Deploy static web apps from microsoft/static-web-apps-examples to AWS
# Uses S3 + CloudFront with Origin Access Control (OAC) for security.
#
# Prerequisites:
#   - AWS CLI configured with credentials for the target account
#   - Node.js >= 20
#   - npm
#
# Usage:
#   export AWS_DEFAULT_REGION=us-east-1
#   ./deploy.sh
#
# This script:
#   1. Clones and builds the static web apps
#   2. Creates private S3 buckets (no public access, SSE-S3 encrypted)
#   3. Creates CloudFront OAC + distributions
#   4. Sets least-privilege bucket policies (CloudFront OAC only)
#   5. Uploads build artifacts
#   6. Reports URLs
#
# Security:
#   - S3 buckets: Block all public access, SSE-S3 encryption at rest
#   - CloudFront: TLS 1.2+ in transit, OAC for S3 access
#   - IAM: Least-privilege bucket policies scoped to specific distributions
#   - SPA routing: Custom 403/404 error responses for client-side routing

set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-us-east-1}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
PREFIX="swa-examples"
BUILD_DIR="/tmp/swa-build-$$"

echo "=== Static Web Apps Deployment ==="
echo "Account: $ACCOUNT_ID"
echo "Region:  $REGION"
echo ""

# --- Phase 1: Clone and Build ---
echo "=== Phase 1: Building Apps ==="
mkdir -p "$BUILD_DIR"

# React Shop at Home
echo "  Building React Shop at Home..."
git clone --depth 1 https://github.com/azure-template-resources/shopathome-react "$BUILD_DIR/shopathome-react" 2>/dev/null
cd "$BUILD_DIR/shopathome-react/react-app"
npm ci --silent 2>/dev/null
npm install eslint-config-prettier --save-dev --silent 2>/dev/null
DISABLE_ESLINT_PLUGIN=true CI=true npm run build --silent 2>/dev/null
REACT_BUILD="$BUILD_DIR/shopathome-react/react-app/build"
echo "  Done."

# Vite Microservices Website
echo "  Building Vite Microservices..."
git clone --depth 1 https://github.com/Azure-Samples/nodejs-microservices "$BUILD_DIR/nodejs-microservices" 2>/dev/null
cd "$BUILD_DIR/nodejs-microservices/packages/website"
npm install --silent 2>/dev/null
npx vite build --silent 2>/dev/null
VITE_BUILD="$BUILD_DIR/nodejs-microservices/packages/website/dist"
echo "  Done."

# Showcase (static HTML)
SHOWCASE_BUILD="$(dirname "$0")/showcase"
echo "  Showcase (no build needed)."

# --- Phase 2: Create S3 Buckets ---
echo ""
echo "=== Phase 2: Creating S3 Buckets ==="
APPS=("react-shopathome" "vite-microservices" "showcase")

for app in "${APPS[@]}"; do
  BUCKET="${PREFIX}-${app}-${ACCOUNT_ID}"
  echo "  Creating: $BUCKET"

  if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
    echo "    Already exists."
  else
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null
  fi

  aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

  aws s3api put-bucket-encryption --bucket "$BUCKET" --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'
done

# --- Phase 3: Create CloudFront OAC ---
echo ""
echo "=== Phase 3: CloudFront OAC ==="
OAC_NAME="${PREFIX}-oac"
OAC_ID=$(aws cloudfront list-origin-access-controls \
  --query "OriginAccessControlList.Items[?Name=='${OAC_NAME}'].Id" --output text 2>/dev/null || echo "")

if [ -z "$OAC_ID" ] || [ "$OAC_ID" = "None" ]; then
  OAC_RESULT=$(aws cloudfront create-origin-access-control --origin-access-control-config "{
    \"Name\": \"${OAC_NAME}\",
    \"Description\": \"OAC for static web app examples\",
    \"SigningProtocol\": \"sigv4\",
    \"SigningBehavior\": \"always\",
    \"OriginAccessControlOriginType\": \"s3\"
  }")
  OAC_ID=$(echo "$OAC_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['OriginAccessControl']['Id'])")
  echo "  Created OAC: $OAC_ID"
else
  echo "  OAC exists: $OAC_ID"
fi

# --- Phase 4: Create CloudFront Distributions ---
echo ""
echo "=== Phase 4: CloudFront Distributions ==="
declare -A DIST_URLS

for app in "${APPS[@]}"; do
  BUCKET="${PREFIX}-${app}-${ACCOUNT_ID}"
  ORIGIN_DOMAIN="${BUCKET}.s3.${REGION}.amazonaws.com"

  EXISTING_DIST=$(aws cloudfront list-distributions \
    --query "DistributionList.Items[?Origins.Items[0].DomainName=='${ORIGIN_DOMAIN}'].DomainName" \
    --output text 2>/dev/null || echo "")

  if [ -n "$EXISTING_DIST" ] && [ "$EXISTING_DIST" != "None" ]; then
    echo "  $app: https://${EXISTING_DIST} (existing)"
    DIST_URLS[$app]="https://${EXISTING_DIST}"
    continue
  fi

  CALLER_REF="swa-${app}-$(date +%s)"
  DIST_RESULT=$(aws cloudfront create-distribution --distribution-config "{
    \"CallerReference\": \"${CALLER_REF}\",
    \"Comment\": \"Static Web App: ${app}\",
    \"Enabled\": true,
    \"DefaultRootObject\": \"index.html\",
    \"Origins\": {
      \"Quantity\": 1,
      \"Items\": [{
        \"Id\": \"S3-${BUCKET}\",
        \"DomainName\": \"${ORIGIN_DOMAIN}\",
        \"OriginAccessControlId\": \"${OAC_ID}\",
        \"S3OriginConfig\": {\"OriginAccessIdentity\": \"\"}
      }]
    },
    \"DefaultCacheBehavior\": {
      \"TargetOriginId\": \"S3-${BUCKET}\",
      \"ViewerProtocolPolicy\": \"redirect-to-https\",
      \"AllowedMethods\": {\"Quantity\": 2, \"Items\": [\"GET\", \"HEAD\"], \"CachedMethods\": {\"Quantity\": 2, \"Items\": [\"GET\", \"HEAD\"]}},
      \"CachePolicyId\": \"658327ea-f89d-4fab-a63d-7e88639e58f6\",
      \"Compress\": true
    },
    \"CustomErrorResponses\": {
      \"Quantity\": 2,
      \"Items\": [
        {\"ErrorCode\": 403, \"ResponsePagePath\": \"/index.html\", \"ResponseCode\": \"200\", \"ErrorCachingMinTTL\": 10},
        {\"ErrorCode\": 404, \"ResponsePagePath\": \"/index.html\", \"ResponseCode\": \"200\", \"ErrorCachingMinTTL\": 10}
      ]
    },
    \"ViewerCertificate\": {\"CloudFrontDefaultCertificate\": true, \"MinimumProtocolVersion\": \"TLSv1.2_2021\"},
    \"PriceClass\": \"PriceClass_100\",
    \"HttpVersion\": \"http2and3\"
  }")

  DIST_ID=$(echo "$DIST_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['Distribution']['Id'])")
  DIST_DOMAIN=$(echo "$DIST_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['Distribution']['DomainName'])")
  DIST_URLS[$app]="https://${DIST_DOMAIN}"
  echo "  $app: https://${DIST_DOMAIN}"

  # Set bucket policy
  aws s3api put-bucket-policy --bucket "$BUCKET" --policy "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [{
      \"Sid\": \"AllowCloudFrontServicePrincipalReadOnly\",
      \"Effect\": \"Allow\",
      \"Principal\": {\"Service\": \"cloudfront.amazonaws.com\"},
      \"Action\": \"s3:GetObject\",
      \"Resource\": \"arn:aws:s3:::${BUCKET}/*\",
      \"Condition\": {\"StringEquals\": {\"AWS:SourceArn\": \"arn:aws:cloudfront::${ACCOUNT_ID}:distribution/${DIST_ID}\"}}
    }]
  }"
done

# --- Phase 5: Upload Artifacts ---
echo ""
echo "=== Phase 5: Uploading Artifacts ==="
aws s3 sync "$REACT_BUILD" "s3://${PREFIX}-react-shopathome-${ACCOUNT_ID}/" --delete --quiet
echo "  react-shopathome: uploaded"

aws s3 sync "$VITE_BUILD" "s3://${PREFIX}-vite-microservices-${ACCOUNT_ID}/" --delete --quiet
echo "  vite-microservices: uploaded"

aws s3 sync "$SHOWCASE_BUILD" "s3://${PREFIX}-showcase-${ACCOUNT_ID}/" --delete --quiet
echo "  showcase: uploaded"

# --- Done ---
echo ""
echo "=========================================="
echo "  DEPLOYMENT COMPLETE"
echo "=========================================="
echo ""
echo "Working URLs (allow 5-10 min for new distributions):"
for app in "${APPS[@]}"; do
  echo "  ${app}: ${DIST_URLS[$app]}"
done
echo ""
echo "Security:"
echo "  - S3: Private, SSE-S3 encrypted, public access blocked"
echo "  - CloudFront: TLS 1.2+, OAC, HTTP/2+3, PriceClass_100"
echo "  - IAM: Bucket policies scoped to specific distribution ARNs"
echo ""

# Cleanup build dir
rm -rf "$BUILD_DIR"
