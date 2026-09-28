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
#   1. The shared agent-worker image its KEDA
#      ScaledJobs run.
#      Terraform only references the :latest tag; it never validates it, so a
#      missing image surfaces as ImagePullBackOff on the FIRST real agent run.
#   2. The webhook Lambda zip in S3, which terraform reads at PLAN time
#      (data.aws_s3_object) — apply fails outright without it.
#
# This script does all three as one cohesive, idempotent, re-runnable step
# (mirrors modules/agent-factory/scripts/deploy-gateway.sh):
#   [1/3] build adp-agent-runtime via CodeBuild
#   [2/3] package + upload the webhook Lambda zip to S3
#   [3/3] terraform apply the webhook-ingress stack
#
# After this, run register-github-app.sh to wire GitHub (creates the App, sets
# the webhook secret, etc.) and install the App on a repo.
#
# Usage:
#   ./deploy-webhook-ingress.sh [--env dev] [--region us-east-1] [--dry-run]
#                               [--skip-image] [--skip-lambda] [--skip-terraform]
#                               [--update] [--confirm-destructive]
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
UPDATE_MODE=false
CONFIRM_DESTRUCTIVE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)            ENVIRONMENT="$2"; shift 2 ;;
    --region)         AWS_REGION="$2"; shift 2 ;;
    --update)         UPDATE_MODE=true; shift ;;
    --confirm-destructive) CONFIRM_DESTRUCTIVE=true; shift ;;
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
source "$REPO_ROOT/platform/scripts/terraform-update.sh"

# Resolve account / region / bucket (mirror build-lambda-layers.sh).
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
if [ -n "${ADP_ACCOUNT_ID:-}" ] && [ "$ADP_ACCOUNT_ID" != "$ACCOUNT_ID" ]; then
  fail "Target account $ADP_ACCOUNT_ID does not match caller $ACCOUNT_ID"
fi
STATE_BUCKET="${ADP_STATE_BUCKET:-adp-terraform-state-${ACCOUNT_ID}}"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
if [ "$UPDATE_MODE" = true ]; then
  IMAGE_TAG="${IMAGE_TAG:-$(git -C "$REPO_ROOT" rev-parse HEAD)}"
  if [ "$DRY_RUN" = false ] && [ -z "${UPGRADE_RUN_DIR:-}" ]; then
    export UPGRADE_RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/adp-upgrade-${ACCOUNT_ID}.XXXXXX")"
    python3 "$REPO_ROOT/platform/scripts/upgrade-state.py" prepare --directory "$UPGRADE_RUN_DIR" \
      --account "$ACCOUNT_ID" --environment "$ENVIRONMENT" --region "$AWS_REGION"
    source "$UPGRADE_RUN_DIR/context.env"
  fi
  if [ "$DRY_RUN" = false ]; then
    case ",${UPGRADE_MODULES:-}," in *,webhook-ingress,*) ;; *) fail "--update requires existing webhook-ingress state" ;; esac
  fi
  WEBHOOK_UPDATE_VAR_FILE="${ADP_WEBHOOK_UPDATE_TFVARS:-}"
  if [ -z "$WEBHOOK_UPDATE_VAR_FILE" ]; then
    for suffix in tfvars tfvars.json; do
      candidate="$REPO_ROOT/environments/$ENVIRONMENT/modules/webhook-ingress.$suffix"
      if [ -f "$candidate" ]; then
        [ -z "$WEBHOOK_UPDATE_VAR_FILE" ] || fail "Multiple webhook update tfvars files; select one explicitly"
        WEBHOOK_UPDATE_VAR_FILE="$candidate"
      fi
    done
  fi
  if [ -n "$WEBHOOK_UPDATE_VAR_FILE" ]; then
    WEBHOOK_UPDATE_VAR_FILE=$(terraform_update_var_file "$WEBHOOK_UPDATE_VAR_FILE" \
      "$WEBHOOK_UPDATE_VAR_FILE" "$ACCOUNT_ID") || fail "Webhook update needs target-specific tfvars"
    ok "Webhook update tfvars: $WEBHOOK_UPDATE_VAR_FILE"
  fi
else
  IMAGE_TAG="${IMAGE_TAG:-$(git -C "$REPO_ROOT" rev-parse HEAD)}"
fi

echo "deploy-webhook-ingress: env=$ENVIRONMENT region=$AWS_REGION account=$ACCOUNT_ID bucket=$STATE_BUCKET"
[ "$DRY_RUN" = true ] && warn "DRY RUN — no changes will be made"

if [ "$DRY_RUN" = false ] && { [ "$SKIP_IMAGE" = false ] || [ "$SKIP_TF" = false ]; }; then
  step "Verify Bedrock access before deploying agent runtimes"
  bash "$REPO_ROOT/platform/scripts/enable-bedrock-models.sh" \
    --prepare-and-verify --region "$AWS_REGION"
fi

# ---------------------------------------------------------------------------
# Helper: codebuild-run.sh path (implements source-SHA contract)
# ---------------------------------------------------------------------------
CODEBUILD_RUN="${REPO_ROOT}/platform/scripts/codebuild-run.sh"
[ -x "$CODEBUILD_RUN" ] || [ -f "$CODEBUILD_RUN" ] || fail "codebuild-run.sh not found at $CODEBUILD_RUN"

# ---------------------------------------------------------------------------
# [1/3] Build the shared worker image
# ---------------------------------------------------------------------------
step "[1/3] Build worker image (agent runtime with Codex adapters)"
if [ -n "${ADP_RELEASE_DIR:-}" ]; then
  python3 "$REPO_ROOT/platform/scripts/release/artifacts.py" verify-prepared --directory "$ADP_RELEASE_DIR"
  ok "Using verified release worker images"
elif [ "$SKIP_IMAGE" = true ]; then
  warn "Skipping image build (--skip-image)."
elif [ "$DRY_RUN" = true ]; then
  echo "  [dry-run] codebuild-run.sh adp-${ENVIRONMENT}-agent-runtime (with source-location-override)"
else
  # Use codebuild-run.sh which handles the source-SHA contract:
  # zips source → uploads to unique S3 key → passes --source-location-override + ADP_SOURCE_SHA
  ADP_RELEASE_BUILD=true SOURCE_SHA="$IMAGE_TAG" STATE_BUCKET="$STATE_BUCKET" AWS_REGION="$AWS_REGION" \
    bash "$CODEBUILD_RUN" "adp-${ENVIRONMENT}-agent-runtime" \
      "name=AWS_REGION,value=${AWS_REGION},type=PLAINTEXT" \
      "name=ACCOUNT_ID,value=${ACCOUNT_ID},type=PLAINTEXT" \
      "name=REGISTRY,value=${REGISTRY},type=PLAINTEXT" \
      "name=IMAGE_TAG,value=${IMAGE_TAG},type=PLAINTEXT" \
      "name=STATE_BUCKET,value=${STATE_BUCKET},type=PLAINTEXT"
  ok "adp-${ENVIRONMENT}-agent-runtime: SUCCEEDED"
fi

# Resolve and validate the image before Lambda upload, Terraform import or apply.
if [ "$DRY_RUN" = false ] && [ "$SKIP_TF" = false ]; then
  VERIFIED_AGENT_IMAGE=$(python3 "$REPO_ROOT/platform/scripts/resolve-ecr-image.py" \
    "${ADP_RELEASE_AGENT_RUNTIME_IMAGE:-${REGISTRY}/adp-agent-runtime:${IMAGE_TAG}}")
  export TF_VAR_agent_image="$VERIFIED_AGENT_IMAGE"
fi

# ---------------------------------------------------------------------------
# [2/3] Package + upload the webhook Lambda zip
# ---------------------------------------------------------------------------
step "[2/3] Package + upload webhook Lambda zip"
if [ -n "${ADP_RELEASE_DIR:-}" ]; then
  ok "Using verified release Lambda packages already published to S3"
elif [ "$SKIP_LAMBDA" = true ]; then
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

TF_WEBHOOK="${SCRIPT_DIR}/terraform-webhook.sh"
export ADP_ENV="$ENVIRONMENT" AWS_REGION STATE_BUCKET

# Check if gitlab.zip exists in S3; if not, override gitlab_webhook_enabled to
# false so terraform doesn't fail on the missing artifact (Issue #3488).
GITLAB_OVERRIDE=""
if [ "$UPDATE_MODE" = false ] && ! aws s3api head-object --bucket "$STATE_BUCKET" --key "lambda-artifacts/webhook-ingress/gitlab.zip" --region "$AWS_REGION" &>/dev/null; then
  warn "gitlab.zip not found in S3 — overriding gitlab_webhook_enabled=false"
  GITLAB_OVERRIDE='-var=gitlab_webhook_enabled=false'
fi

# Check if the gateway internal-api-key secret exists; if not, override
# enable_adversarial_e2e to false so terraform doesn't fail on the missing
# secret. terraform.tfvars sets enable_adversarial_e2e=true for CI, but the
# auto-loaded tfvars also applies to fresh deploys. (Issue #3488/#3490)
ADVERSARIAL_OVERRIDE=""
if [ "$UPDATE_MODE" = false ] && ! aws secretsmanager describe-secret --secret-id "adp/${ENVIRONMENT}/gateway/internal-api-key" --region "$AWS_REGION" &>/dev/null; then
  warn "internal-api-key secret not found — overriding enable_adversarial_e2e=false"
  ADVERSARIAL_OVERRIDE='-var=enable_adversarial_e2e=false'
fi

# Resolve the internal-api-key ARN so the webhook Lambda can call the gateway's
# /internal/v1/* endpoints. Empty string is safe (Lambda falls back to DDB-only
# identity resolution, but gateway-canonical resolution is disabled).
INTERNAL_API_KEY_OVERRIDE=""
INTERNAL_API_KEY_ARN=$(aws secretsmanager describe-secret \
  --secret-id "adp/${ENVIRONMENT}/gateway/internal-api-key" \
  --query 'ARN' --output text --region "$AWS_REGION" 2>/dev/null || echo "")
if [ -n "$INTERNAL_API_KEY_ARN" ] && [ "$INTERNAL_API_KEY_ARN" != "None" ]; then
  ok "Internal API key: ${INTERNAL_API_KEY_ARN}"
  INTERNAL_API_KEY_OVERRIDE="-var=internal_api_key_arn=${INTERNAL_API_KEY_ARN}"
else
  warn "internal-api-key secret not found — webhook→gateway identity resolution will be disabled"
fi

# Adopt a pre-existing agent bootstrap log group into state (issue #4051).
#
# aws_cloudwatch_log_group.agent_bootstrap is declared unconditionally, but
# agent-worker-image/lib/bootstrap_logger.py calls CreateLogGroup at runtime as
# a fallback. In any environment whose worker has already run, the group exists
# outside state and the first apply dies with ResourceAlreadyExistsException,
# wedging the whole module — that is what happened in dev.
#
# A static TF `import` block cannot express this: it fails in a fresh
# environment where the group does not exist yet. So import conditionally, only
# when the group is present in AWS AND absent from state. Both guards make this
# a clean no-op on re-runs rather than a collision.
BOOTSTRAP_LOG_GROUP="/adp/${ENVIRONMENT}/agent-factory/bootstrap"
import_bootstrap_log_group() {
  local found
  found=$(aws logs describe-log-groups \
    --log-group-name-prefix "$BOOTSTRAP_LOG_GROUP" \
    --query "logGroups[?logGroupName=='${BOOTSTRAP_LOG_GROUP}'].logGroupName | [0]" \
    --output text --region "$AWS_REGION" 2>/dev/null || echo "None")
  if [ "$found" != "$BOOTSTRAP_LOG_GROUP" ]; then
    ok "Bootstrap log group does not exist yet — terraform will create it"
    return 0
  fi
  # Do not pipe Terraform into grep -q under pipefail: grep exits at its first
  # match, Terraform can receive SIGPIPE, and an already-managed group is then
  # incorrectly imported again. State-read failures must also stop the upgrade.
  local managed_addresses
  managed_addresses=$(terraform state list) || fail "Cannot inspect webhook Terraform state"
  if grep -Fxq 'aws_cloudwatch_log_group.agent_bootstrap' <<< "$managed_addresses"; then
    ok "Bootstrap log group already in state — no import needed"
    return 0
  fi
  warn "Bootstrap log group exists in AWS but not in state — importing (#4051)"
  local import_args=()
  if [ "$UPDATE_MODE" = true ]; then
    import_args+=(-var-file=terraform.tfvars)
    [ -z "$WEBHOOK_UPDATE_VAR_FILE" ] || import_args+=("-var-file=$WEBHOOK_UPDATE_VAR_FILE")
    import_args+=(-var-file="$UPGRADE_RUN_DIR/webhook-ingress.tfvars.json")
    import_args+=(-var="environment=$ENVIRONMENT" -var="aws_region=$AWS_REGION")
    terraform import "${import_args[@]}" aws_cloudwatch_log_group.agent_bootstrap "$BOOTSTRAP_LOG_GROUP"
  else
    bash "$TF_WEBHOOK" import aws_cloudwatch_log_group.agent_bootstrap "$BOOTSTRAP_LOG_GROUP"
  fi
  ok "Imported aws_cloudwatch_log_group.agent_bootstrap"
}

if [ "$SKIP_TF" = true ]; then
  warn "Skipping terraform apply (--skip-terraform)."
elif [ "$DRY_RUN" = true ]; then
  echo "  [dry-run] Terraform backend: ${STATE_BUCKET}/${ENVIRONMENT}/modules/webhook-ingress/terraform.tfstate"
  echo "  [dry-run] conditional import of aws_cloudwatch_log_group.agent_bootstrap ($BOOTSTRAP_LOG_GROUP)"
  echo "  [dry-run] terraform apply -var=environment=$ENVIRONMENT -var=gateway_api_url=$GATEWAY_API_URL${GITLAB_OVERRIDE:+ $GITLAB_OVERRIDE}"
else
  (
    cd "${MODULE_ROOT}/infra"
    bash "$TF_WEBHOOK" init -input=false -reconfigure >/dev/null
    TF_ARGS=(
      -var="environment=${ENVIRONMENT}"
      -var="aws_region=${AWS_REGION}"
      -var="agent_image=$VERIFIED_AGENT_IMAGE"
    )
    if [ "$UPDATE_MODE" = false ]; then
      TF_ARGS+=(-var="gateway_api_url=${GATEWAY_API_URL}")
    fi
    import_bootstrap_log_group
    [ -z "$GITLAB_OVERRIDE" ] || TF_ARGS+=("$GITLAB_OVERRIDE")
    [ -z "$ADVERSARIAL_OVERRIDE" ] || TF_ARGS+=("$ADVERSARIAL_OVERRIDE")
    if [ "$UPDATE_MODE" = false ]; then
      [ -z "$INTERNAL_API_KEY_OVERRIDE" ] || TF_ARGS+=("$INTERNAL_API_KEY_OVERRIDE")
    fi
    if [ "$UPDATE_MODE" = true ]; then
      OVERLAY_ARGS=()
      [ -z "$WEBHOOK_UPDATE_VAR_FILE" ] || OVERLAY_ARGS+=("-var-file=$WEBHOOK_UPDATE_VAR_FILE")
      terraform_update_apply webhook-ingress terraform.tfvars ${OVERLAY_ARGS[@]+"${OVERLAY_ARGS[@]}"} "${TF_ARGS[@]}"
    else
      bash "$TF_WEBHOOK" apply "${TF_ARGS[@]}" -input=false -auto-approve
    fi
  )
  ok "webhook-ingress applied"
fi

echo ""
ok "Webhook-ingress deploy complete."
if [ "$UPDATE_MODE" = true ] && [ "$DRY_RUN" = false ]; then
  python3 "$REPO_ROOT/platform/scripts/upgrade-state.py" verify --directory "$UPGRADE_RUN_DIR" --region "$AWS_REGION"
elif [ "$UPDATE_MODE" = false ]; then
  echo "  Next: register-github-app.sh <org> --env ${ENVIRONMENT} (first-time setup only)"
fi
