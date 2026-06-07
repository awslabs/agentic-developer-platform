#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# deploy-webhook-ingress.sh — Deploy the ARC-free webhook agent path
# =============================================================================
# The webhook-ingress stack (API Gateway → github-webhook Lambda → SQS FIFO →
# KEDA ScaledJob → agent-worker pod) is NOT deployed by deploy-all.sh, and it
# has two plan/runtime prerequisites that no other flow builds:
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
# Helper: run a CodeBuild project and wait for completion
# ---------------------------------------------------------------------------
run_codebuild() {
  local project="$1"
  local build_id
  build_id=$(aws codebuild start-build --project-name "$project" --region "$AWS_REGION" \
    --environment-variables-override \
      "name=AWS_REGION,value=${AWS_REGION}" \
      "name=ACCOUNT_ID,value=${ACCOUNT_ID}" \
      "name=REGISTRY,value=${REGISTRY}" \
      "name=IMAGE_TAG,value=${IMAGE_TAG}" \
      "name=STATE_BUCKET,value=${STATE_BUCKET}" \
    --query 'build.id' --output text)
  echo "  build: $build_id"
  while true; do
    local s
    s=$(aws codebuild batch-get-builds --ids "$build_id" --region "$AWS_REGION" \
      --query 'builds[0].buildStatus' --output text 2>/dev/null || echo IN_PROGRESS)
    case "$s" in
      SUCCEEDED) ok "$project: SUCCEEDED"; return 0 ;;
      FAILED|FAULT|STOPPED|TIMED_OUT)
        aws codebuild batch-get-builds --ids "$build_id" --region "$AWS_REGION" \
          --query 'builds[0].logs.deepLink' --output text 2>/dev/null || true
        fail "$project: $s" ;;
      *) sleep 15 ;;
    esac
  done
}

# ---------------------------------------------------------------------------
# [1/3] Build the agent-worker image (adp-agent-runtime)
# ---------------------------------------------------------------------------
step "[1/3] Build agent-worker image (adp-agent-runtime)"
if [ "$SKIP_IMAGE" = true ]; then
  warn "Skipping image build (--skip-image)."
elif [ "$DRY_RUN" = true ]; then
  echo "  [dry-run] zip-source.sh → s3://${STATE_BUCKET}/codebuild/adp-source.zip"
  echo "  [dry-run] run_codebuild adp-${ENVIRONMENT}-agent-runtime"
else
  # CodeBuild reads source from s3://<bucket>/codebuild/adp-source.zip — refresh it.
  bash "${REPO_ROOT}/platform/scripts/zip-source.sh" "$REPO_ROOT" /tmp/adp-source.$$.zip >/dev/null
  aws s3 cp /tmp/adp-source.$$.zip "s3://${STATE_BUCKET}/codebuild/adp-source.zip" --region "$AWS_REGION" >/dev/null
  rm -f /tmp/adp-source.$$.zip
  ok "Source uploaded to s3://${STATE_BUCKET}/codebuild/adp-source.zip"
  run_codebuild "adp-${ENVIRONMENT}-agent-runtime"
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
BACKEND="${REPO_ROOT}/environments/${ENVIRONMENT}/modules/webhook-ingress-backend.tfvars"
if [ "$SKIP_TF" = true ]; then
  warn "Skipping terraform apply (--skip-terraform)."
elif [ "$DRY_RUN" = true ]; then
  echo "  [dry-run] terraform init -backend-config=$BACKEND"
  echo "  [dry-run] terraform apply -var=environment=$ENVIRONMENT"
else
  ( cd "${MODULE_ROOT}/infra" \
    && terraform init -backend-config="$BACKEND" -input=false -reconfigure >/dev/null \
    && terraform apply -var="environment=${ENVIRONMENT}" -input=false -auto-approve )
  ok "webhook-ingress applied"
fi

echo ""
ok "Webhook-ingress deploy complete."
echo "  Next: register-github-app.sh <org> --env ${ENVIRONMENT}  (creates + wires the GitHub App)"
echo "        then install the App on a repo and @mention an agent."
