#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# deploy-broker.sh — Publish the github-auth-broker Lambda code
# =============================================================================
# Terraform (modules/gateway/infra/modules/github-auth-broker) creates the
# broker Lambda with a PLACEHOLDER zip that just returns HTTP 503. The real
# code is normally published by the push-triggered
# .github/workflows/github-auth-broker-deploy.yml — which a fresh self-managed
# deploy never fires. This script is the manual, repeatable equivalent: it
# packages modules/gateway/lambda/github-auth-broker/, uploads it to the state
# bucket, and updates the live Lambda's code.
#
# Run AFTER gateway-infra is applied (the Lambda must exist). Idempotent.
#
# Usage:
#   ./deploy-broker.sh [--env dev] [--region us-east-1] [--dry-run]
#                      [--skip-package] [--skip-update]
#
# Env (flags win; otherwise resolved from config/deployment.yml or runtime):
#   ADP_ENV / --env            environment (default dev)
#   AWS_REGION / --region      region (default us-east-1)
#   ADP_STATE_BUCKET           state bucket (default adp-terraform-state-<account>)
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"               # modules/gateway
REPO_ROOT="$(cd "${MODULE_ROOT}/../.." && pwd)"
LAMBDA_SRC_DIR="${MODULE_ROOT}/lambda/github-auth-broker"

ENVIRONMENT="${ADP_ENV:-dev}"
AWS_REGION="${AWS_REGION:-us-east-1}"
DRY_RUN=false
SKIP_PACKAGE=false
SKIP_UPDATE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)          ENVIRONMENT="$2"; shift 2 ;;
    --region)       AWS_REGION="$2"; shift 2 ;;
    --dry-run)      DRY_RUN=true; shift ;;
    --skip-package) SKIP_PACKAGE=true; shift ;;
    --skip-update)  SKIP_UPDATE=true; shift ;;
    -h|--help)      sed -n '4,30p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}⚠${NC} $1"; }
fail() { echo -e "${RED}✗${NC} $1"; exit 1; }
run()  { if [ "$DRY_RUN" = true ]; then echo -e "${BLUE}[dry-run]${NC} $*"; else eval "$@"; fi; }

command -v aws &>/dev/null || fail "AWS CLI not installed"
command -v pip &>/dev/null || command -v pip3 &>/dev/null || fail "pip not installed"
PIP=$(command -v pip3 || command -v pip)
[ -d "$LAMBDA_SRC_DIR" ] || fail "Broker source dir not found: $LAMBDA_SRC_DIR"

# Resolve account / state bucket (mirror build-lambda-layers.sh).
if [ -z "${ADP_STATE_BUCKET:-}" ]; then
  ACCOUNT_ID="${ADP_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
  STATE_BUCKET="adp-terraform-state-${ACCOUNT_ID}"
else
  STATE_BUCKET="$ADP_STATE_BUCKET"
fi

FUNCTION_NAME="bedrockgw-${ENVIRONMENT}-github-auth-broker"
S3_KEY="lambda-artifacts/github-auth-broker/broker.zip"

echo ""
echo "deploy-broker: env=$ENVIRONMENT region=$AWS_REGION bucket=$STATE_BUCKET fn=$FUNCTION_NAME"
echo ""

# -----------------------------------------------------------------------------
# 1. Package — replicate github-auth-broker-deploy.yml exactly
# -----------------------------------------------------------------------------
BUILD_DIR="${LAMBDA_SRC_DIR}/build"
ZIP_PATH="${LAMBDA_SRC_DIR}/broker.zip"
if [ "$SKIP_PACKAGE" = true ]; then
  warn "Skipping packaging (--skip-package); expecting existing $ZIP_PATH"
else
  echo "Packaging broker Lambda (linux/py3.12)..."
  run "rm -rf '$BUILD_DIR' && mkdir -p '$BUILD_DIR'"
  # Install deps targeting the Lambda runtime (manylinux/py3.12), binary-only.
  run "'$PIP' install --target '$BUILD_DIR' \
        --platform manylinux2014_x86_64 --only-binary=:all: \
        --implementation cp --python-version 3.12 \
        -r '${LAMBDA_SRC_DIR}/requirements.txt' --quiet"
  # Copy the handler + sibling modules flat into the build dir.
  if [ "$DRY_RUN" = false ]; then
    ( cd "$LAMBDA_SRC_DIR" && for f in *.py; do [ -f "$f" ] && cp "$f" build/; done )
    ( cd "$BUILD_DIR" && find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true; \
      rm -f "$ZIP_PATH"; zip -r -q "$ZIP_PATH" . )
  else
    echo -e "${BLUE}[dry-run]${NC} cp *.py -> build/ ; zip -r broker.zip ."
  fi
  [ "$DRY_RUN" = false ] && ok "Built $(du -h "$ZIP_PATH" 2>/dev/null | cut -f1) $ZIP_PATH"
fi

# -----------------------------------------------------------------------------
# 2. Upload to S3
# -----------------------------------------------------------------------------
run "aws s3 cp '$ZIP_PATH' 's3://${STATE_BUCKET}/${S3_KEY}' --region '$AWS_REGION'"
[ "$DRY_RUN" = false ] && ok "Uploaded to s3://${STATE_BUCKET}/${S3_KEY}"

# -----------------------------------------------------------------------------
# 3. Update the live Lambda code from S3
# -----------------------------------------------------------------------------
if [ "$SKIP_UPDATE" = true ]; then
  warn "Skipping function-code update (--skip-update)."
elif [ "$DRY_RUN" = true ]; then
  echo -e "${BLUE}[dry-run]${NC} aws lambda update-function-code --function-name $FUNCTION_NAME --s3-bucket $STATE_BUCKET --s3-key $S3_KEY"
elif ! aws lambda get-function --function-name "$FUNCTION_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
  fail "Lambda $FUNCTION_NAME not found — apply gateway-infra first."
else
  aws lambda update-function-code \
    --function-name "$FUNCTION_NAME" \
    --s3-bucket "$STATE_BUCKET" --s3-key "$S3_KEY" \
    --region "$AWS_REGION" --query 'CodeSize' --output text >/dev/null
  aws lambda wait function-updated --function-name "$FUNCTION_NAME" --region "$AWS_REGION"
  NEW_SIZE=$(aws lambda get-function-configuration --function-name "$FUNCTION_NAME" --region "$AWS_REGION" --query CodeSize --output text)
  ok "Updated $FUNCTION_NAME code (CodeSize=$NEW_SIZE bytes)"
fi

echo ""
ok "Broker deploy complete."
echo "  Verify: curl -s -o /dev/null -w '%{http_code}' <apigw>/auth/github/start  (expect 302)"
