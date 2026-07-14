#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# deploy-webhook-ingress.sh — Deploy the ARC-free webhook agent path
# =============================================================================
# The webhook-ingress stack (API Gateway → github-webhook Lambda → SQS FIFO →
# KEDA ScaledJob → agent-worker pod) is deployed by deploy-all.sh Step 10/11,
# or run standalone. It has two plan/runtime prerequisites that no other flow
# builds:
#
#   1. The agent-worker image (adp-agent-runtime) the KEDA ScaledJob runs.
#      Terraform only references the :latest tag; it never validates it, so a
#      missing image surfaces as ImagePullBackOff on the FIRST real agent run.
#   2. The webhook Lambda zip in S3, which terraform reads at PLAN time
#      (data.aws_s3_object) — apply fails outright without it.
#
# This script does all three as one cohesive, idempotent, re-runnable step
# (mirrors modules/agent-factory/scripts/deploy-gateway.sh):
#   [1/3] build adp-agent-runtime image via CodeBuild
#   [2/3] package + upload the webhook Lambda zip to S3
#   [3/3] terraform apply the webhook-ingress stack
#
# After this, run register-github-app.sh to wire GitHub (creates the App, sets
# the webhook secret, etc.) and install the App on a repo.
#
# Usage:
#   ./deploy-webhook-ingress.sh [--env dev] [--region us-east-1] [--dry-run]
#                               [--skip-image] [--skip-lambda] [--skip-terraform]
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"               # .../webhook-ingress
REPO_ROOT="$(cd "${MODULE_ROOT}/../../.." && pwd)"

ENVIRONMENT="${ADP_ENV:-dev}"
AWS_REGION="${AWS_REGION:-us-east-1}"
DRY_RUN=false
SKIP_IMAGE=false
SKIP_LAMBDA=false
SKIP_TF=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)            ENVIRONMENT="$2"; shift 2 ;;
    --region)         AWS_REGION="$2"; shift 2 ;;
    --dry-run)        DRY_RUN=true; shift ;;
    --skip-image)     SKIP_IMAGE=true; shift ;;
    --skip-lambda)    SKIP_LAMBDA=true; shift ;;
    --skip-terraform) SKIP_TF=true; shift ;;
    -h|--help)        sed -n '4,33p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}⚠${NC} $1"; }
fail() { echo -e "${RED}✗${NC} $1"; exit 1; }
step() { echo -e "\n${BLUE}$1${NC}"; }

command -v aws &>/dev/null || fail "AWS CLI not installed"

# Resolve account / region / bucket (mirror build-lambda-layers.sh).
ACCOUNT_ID="${ADP_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
STATE_BUCKET="${ADP_STATE_BUCKET:-adp-terraform-state-${ACCOUNT_ID}}"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
IMAGE_TAG="${IMAGE_TAG:-latest}"

echo "deploy-webhook-ingress: env=$ENVIRONMENT region=$AWS_REGION account=$ACCOUNT_ID bucket=$STATE_BUCKET"
[ "$DRY_RUN" = true ] && warn "DRY RUN — no changes will be made"

# ---------------------------------------------------------------------------
# Helper: codebuild-run.sh path (implements source-SHA contract)
# ---------------------------------------------------------------------------
CODEBUILD_RUN="${REPO_ROOT}/platform/scripts/codebuild-run.sh"
[ -x "$CODEBUILD_RUN" ] || [ -f "$CODEBUILD_RUN" ] || fail "codebuild-run.sh not found at $CODEBUILD_RUN"

# ---------------------------------------------------------------------------
# [1/3] Build the agent-worker image (adp-agent-runtime)
# ---------------------------------------------------------------------------
step "[1/3] Build agent-worker image (adp-agent-runtime)"
if [ "$SKIP_IMAGE" = true ]; then
  warn "Skipping image build (--skip-image)."
elif [ "$DRY_RUN" = true ]; then
  echo "  [dry-run] codebuild-run.sh adp-${ENVIRONMENT}-agent-runtime (with source-location-override)"
else
  # Use codebuild-run.sh which handles the source-SHA contract:
  # zips source → uploads to unique S3 key → passes --source-location-override + ADP_SOURCE_SHA
  STATE_BUCKET="$STATE_BUCKET" AWS_REGION="$AWS_REGION" \
    bash "$CODEBUILD_RUN" "adp-${ENVIRONMENT}-agent-runtime" \
      "name=AWS_REGION,value=${AWS_REGION},type=PLAINTEXT" \
      "name=ACCOUNT_ID,value=${ACCOUNT_ID},type=PLAINTEXT" \
      "name=REGISTRY,value=${REGISTRY},type=PLAINTEXT" \
      "name=IMAGE_TAG,value=${IMAGE_TAG},type=PLAINTEXT" \
      "name=STATE_BUCKET,value=${STATE_BUCKET},type=PLAINTEXT"
  ok "adp-${ENVIRONMENT}-agent-runtime: SUCCEEDED"
fi

# ---------------------------------------------------------------------------
# [2/3] Package + upload the webhook Lambda zip
# ---------------------------------------------------------------------------
step "[2/3] Package + upload webhook Lambda zip"
if [ "$SKIP_LAMBDA" = true ]; then
  warn "Skipping Lambda packaging (--skip-lambda)."
elif [ "$DRY_RUN" = true ]; then
  echo "  [dry-run] bash scripts/package-lambdas.sh"
  echo "  [dry-run] aws s3 cp dist/github.zip s3://${STATE_BUCKET}/lambda-artifacts/webhook-ingress/github.zip"
else
  ( cd "$MODULE_ROOT" && bash scripts/package-lambdas.sh >/dev/null )
  [ -f "${MODULE_ROOT}/dist/github.zip" ] || fail "package-lambdas.sh did not produce dist/github.zip"
  aws s3 cp "${MODULE_ROOT}/dist/github.zip" \
    "s3://${STATE_BUCKET}/lambda-artifacts/webhook-ingress/github.zip" --region "$AWS_REGION" >/dev/null
  ok "Uploaded webhook Lambda zip to s3://${STATE_BUCKET}/lambda-artifacts/webhook-ingress/github.zip"
fi

# ---------------------------------------------------------------------------
# [3/3] terraform apply the webhook-ingress stack
# ---------------------------------------------------------------------------
step "[3/3] terraform apply webhook-ingress"

# Read gateway API GW URL from SSM for the resolve-installation fallback path.
# Published by gateway-infra terraform; empty string is safe (disables fallback).
GATEWAY_API_URL=$(aws ssm get-parameter \
  --name "/adp/${ENVIRONMENT}/gateway/apigw-invoke-url" \
  --query "Parameter.Value" --output text --region "$AWS_REGION" 2>/dev/null || echo "")
if [ -n "$GATEWAY_API_URL" ]; then
  ok "Gateway API URL: ${GATEWAY_API_URL}"
else
  warn "Gateway API URL not found in SSM — resolve-installation fallback will be disabled"
fi

BACKEND="${REPO_ROOT}/environments/${ENVIRONMENT}/modules/webhook-ingress-backend.tfvars"

# Check if gitlab.zip exists in S3; if not, override gitlab_webhook_enabled to
# false so terraform doesn't fail on the missing artifact (Issue #3488).
GITLAB_OVERRIDE=""
if ! aws s3api head-object --bucket "$STATE_BUCKET" --key "lambda-artifacts/webhook-ingress/gitlab.zip" --region "$AWS_REGION" &>/dev/null; then
  warn "gitlab.zip not found in S3 — overriding gitlab_webhook_enabled=false"
  GITLAB_OVERRIDE='-var=gitlab_webhook_enabled=false'
fi

# Check if the gateway internal-api-key secret exists; if not, override
# enable_adversarial_e2e to false so terraform doesn't fail on the missing
# secret. terraform.tfvars sets enable_adversarial_e2e=true for CI, but the
# auto-loaded tfvars also applies to fresh deploys. (Issue #3488/#3490)
ADVERSARIAL_OVERRIDE=""
if ! aws secretsmanager describe-secret --secret-id "adp/${ENVIRONMENT}/gateway/internal-api-key" --region "$AWS_REGION" &>/dev/null; then
  warn "internal-api-key secret not found — overriding enable_adversarial_e2e=false"
  ADVERSARIAL_OVERRIDE='-var=enable_adversarial_e2e=false'
fi

if [ "$SKIP_TF" = true ]; then
  warn "Skipping terraform apply (--skip-terraform)."
elif [ "$DRY_RUN" = true ]; then
  echo "  [dry-run] terraform init -backend-config=$BACKEND"
  echo "  [dry-run] terraform apply -var=environment=$ENVIRONMENT -var=gateway_api_url=$GATEWAY_API_URL${GITLAB_OVERRIDE:+ $GITLAB_OVERRIDE}"
else
  # shellcheck disable=SC2086
  ( cd "${MODULE_ROOT}/infra" \
    && terraform init -backend-config="$BACKEND" -input=false -reconfigure >/dev/null \
    && terraform apply \
         -var="environment=${ENVIRONMENT}" \
         -var="gateway_api_url=${GATEWAY_API_URL}" \
         $GITLAB_OVERRIDE \
         $ADVERSARIAL_OVERRIDE \
         -input=false -auto-approve )
  ok "webhook-ingress applied"
fi

echo ""
ok "Webhook-ingress deploy complete."
echo "  Next: register-github-app.sh <org> --env ${ENVIRONMENT}  (creates + wires the GitHub App)"
echo "        then install the App on a repo and @mention an agent."
