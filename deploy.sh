#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# deploy.sh — End-to-end ADP deployment (gateway + webhook agents)
# =============================================================================
# A single script that deploys ADP from zero to a working platform with agents.
# Wraps deploy-all.sh (Phases 1-8: infra, gateway, frontend, ALB wire, broker,
# admin bootstrap) + webhook-ingress (Phase 9).
#
# GitHub App registration is handled via the platform UI after deployment:
#   Settings → Connections → 'Set up GitHub App'
#
# Usage:
#   export AWS_PROFILE=<profile> AWS_REGION=<region>
#   ./deploy.sh [options]
#
# Options:
#   --env ENV              Environment (default: dev)
#   --region REGION        Target AWS region (default: us-east-1, or AWS_REGION env)
#   --local                Use local Docker builds instead of CodeBuild
#   --skip-agents          Deploy gateway only, skip webhook agent stack
#   --dry-run              Show what would be done without executing
#
# The script automatically rewrites environments/*.tfvars to match the target
# region and account. Works with any AWS region (us-east-1, us-west-2, eu-west-1, etc.)
#
# Prerequisites: aws-cli v2, terraform >= 1.14, kubectl, node >= 22, docker (if --local)
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$SCRIPT_DIR"
PLATFORM_SCRIPTS="$ROOT_DIR/platform/scripts"

DEPLOY_START=$(date +%s)
echo "============================================================================="
echo "Deploy started at: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"

ENVIRONMENT="${ENVIRONMENT:-dev}"
AWS_REGION="${AWS_REGION:-us-east-1}"
LOCAL_MODE=false
SKIP_AGENTS=false
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)           ENVIRONMENT="$2"; shift 2 ;;
    --region)        AWS_REGION="$2"; shift 2 ;;
    --local)         LOCAL_MODE=true; shift ;;
    --skip-agents)   SKIP_AGENTS=true; shift ;;
    --dry-run)       DRY_RUN=true; shift ;;
    -h|--help)       sed -n '4,26p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

# =============================================================================
# Interactive prompts for required arguments not provided
# =============================================================================
if [ -t 0 ] && { [ -z "$AWS_REGION" ] || [ "$AWS_REGION" = "us-east-1" ]; }; then
  echo -n "AWS region [us-east-1]: "
  read -r INPUT_REGION
  [ -n "$INPUT_REGION" ] && AWS_REGION="$INPUT_REGION"
fi

export AWS_REGION ENVIRONMENT
export AWS_PAGER=""  # Disable interactive pager

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
step()  { echo -e "\n${BLUE}════════════════════════════════════════════════════${NC}"; echo -e "${BLUE}  $1${NC}"; echo -e "${BLUE}════════════════════════════════════════════════════${NC}\n"; }
ok()    { echo -e "${GREEN}✓${NC} $1"; }
warn()  { echo -e "${YELLOW}⚠${NC} $1"; }
fail()  { echo -e "${RED}✗${NC} $1"; exit 1; }
run()   { if [ "$DRY_RUN" = true ]; then echo -e "${BLUE}[dry-run]${NC} $*"; else eval "$@"; fi; }

# =============================================================================
# Preflight
# =============================================================================
step "Phase 0: Validate environment"

command -v aws >/dev/null 2>&1       || fail "aws CLI not found"
command -v terraform >/dev/null 2>&1 || fail "terraform not found"
command -v kubectl >/dev/null 2>&1   || fail "kubectl not found"
command -v node >/dev/null 2>&1      || fail "node not found (>= 22 required)"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text) || fail "AWS credentials not configured"
CALLER_ARN=$(aws sts get-caller-identity --query Arn --output text)

echo "Account:     $ACCOUNT_ID"
echo "Caller:      $CALLER_ARN"
echo "Region:      $AWS_REGION"
echo "Environment: $ENVIRONMENT"
echo "Scope:       $([ "$SKIP_AGENTS" = true ] && echo 'gateway-only' || echo 'full (gateway + agents)')"
echo ""

if [ "$DRY_RUN" = true ]; then
  warn "DRY RUN — no changes will be made"
  echo ""
fi

# =============================================================================
# Region configuration: rewrite tfvars to match target region
# =============================================================================
step "Configuring target region: $AWS_REGION"

# The tfvars ship with us-east-1 as default. Rewrite all region references
# to match the target AWS_REGION so terraform deploys to the right place.
if [ "$DRY_RUN" = false ]; then
  # Detect sed variant (GNU vs BSD/macOS)
  if sed --version >/dev/null 2>&1; then
    SED_INPLACE=(sed -i)
  else
    SED_INPLACE=(sed -i '')
  fi

  # Rewrite aws_region in all tfvars files
  find "$ROOT_DIR/environments" -name "*.tfvars" -exec "${SED_INPLACE[@]}" \
    -e "s/aws_region[[:space:]]*=.*/aws_region  = \"${AWS_REGION}\"/" \
    -e "s/region[[:space:]]*=.*/region         = \"${AWS_REGION}\"/" {} \;

  # Rewrite account ID in state bucket references
  find "$ROOT_DIR/environments" -name "*.tfvars" -exec "${SED_INPLACE[@]}" \
    -e "s/ACCOUNT_ID/${ACCOUNT_ID}/g" \
    -e "s/adp-terraform-state-[0-9]\{12\}/adp-terraform-state-${ACCOUNT_ID}/g" {} \;

  # Update extra_cluster_admin_principal_arns to reference the deployer's account
  "${SED_INPLACE[@]}" \
    -e "s|arn:aws:iam::[0-9]\{12\}:|arn:aws:iam::${ACCOUNT_ID}:|g" \
    "$ROOT_DIR/environments/${ENVIRONMENT}/platform.tfvars"

  # Fix agent_context_ingestion_queue_arn to use correct account + region
  "${SED_INPLACE[@]}" \
    -e "s|arn:aws:sqs:[a-z0-9-]*:[0-9]\{12\}:|arn:aws:sqs:${AWS_REGION}:${ACCOUNT_ID}:|g" \
    "$ROOT_DIR/environments/${ENVIRONMENT}/modules/gateway.tfvars"

  # Blank out gitlab_origin vars if the ALB doesn't exist in this account
  # (GitLab is only deployed in the platform account, not customer accounts)
  GITLAB_ALB_EXISTS=$(aws elbv2 describe-load-balancers --region "$AWS_REGION" \
    --query "LoadBalancers[?contains(LoadBalancerName,'gitlab')].LoadBalancerArn" \
    --output text 2>/dev/null || echo "")
  if [ -z "$GITLAB_ALB_EXISTS" ]; then
    "${SED_INPLACE[@]}" \
      -e 's|^gitlab_origin_dns.*|gitlab_origin_dns = ""|' \
      -e 's|^gitlab_origin_arn.*|gitlab_origin_arn = ""|' \
      "$ROOT_DIR/environments/${ENVIRONMENT}/modules/gateway.tfvars"
    ok "GitLab origin disabled (no GitLab ALB in account $ACCOUNT_ID)"
  fi

  ok "All tfvars updated for region=$AWS_REGION account=$ACCOUNT_ID"
else
  echo "[dry-run] Would rewrite environments/**/*.tfvars with region=$AWS_REGION account=$ACCOUNT_ID"
fi

# Create the IAM placeholder role terraform expects (adp-dev-agent-runner-role)
if ! aws iam get-role --role-name "adp-${ENVIRONMENT}-agent-runner-role" >/dev/null 2>&1; then
  if [ "$DRY_RUN" = false ]; then
    aws iam create-role --role-name "adp-${ENVIRONMENT}-agent-runner-role" \
      --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"codebuild.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
      --description "ADP CI runner role placeholder" >/dev/null 2>&1
    ok "Created placeholder IAM role: adp-${ENVIRONMENT}-agent-runner-role"
  fi
else
  ok "IAM role adp-${ENVIRONMENT}-agent-runner-role already exists"
fi

# =============================================================================
# Phase 1-8: deploy-all.sh --gateway-only
# =============================================================================
step "Phases 1-8: Platform + Gateway (deploy-all.sh --gateway-only)"

DEPLOY_FLAGS="--gateway-only"
[ "$LOCAL_MODE" = true ] && DEPLOY_FLAGS="$DEPLOY_FLAGS --local"

# Clear stale .terraform state from prior deployments
echo "Clearing stale .terraform backend configs..."
find "$ROOT_DIR" -path "*/.terraform/terraform.tfstate" -delete 2>/dev/null || true

run "bash '$PLATFORM_SCRIPTS/deploy-all.sh' $DEPLOY_FLAGS"
ok "Phases 1-8 complete (platform + gateway infra + backend + frontend + ALB wire + broker + admin bootstrap)"

# =============================================================================
# Phase 9: Webhook agent stack (optional)
# =============================================================================
if [ "$SKIP_AGENTS" = true ]; then
  warn "Skipping agent stack (--skip-agents)"
else
  step "Phase 9: Webhook agent stack (Lambda + SQS + KEDA + agent-runtime image)"

  # Note: deploy-webhook-ingress.sh handles fresh-account absences itself
  # (missing gitlab.zip / internal-api-key secret → features auto-disabled
  # via terraform var overrides; see PRs #3492/#3504).
  run "bash '$ROOT_DIR/modules/agent-factory/webhook-ingress/scripts/deploy-webhook-ingress.sh' --env '$ENVIRONMENT' --region '$AWS_REGION'"
  ok "Webhook ingress stack deployed"
fi

# =============================================================================
# Verification
# =============================================================================
step "Verification"

if [ "$DRY_RUN" = false ]; then
  CF=$(aws ssm get-parameter --name "/adp/${ENVIRONMENT}/gateway/cloudfront-domain" \
    --query Parameter.Value --output text --region "$AWS_REGION" 2>/dev/null || echo "")

  if [ -n "$CF" ] && [ "$CF" != "None" ]; then
    FRONTEND_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "https://$CF/" 2>/dev/null || echo "000")
    HEALTH_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "https://$CF/api/health" 2>/dev/null || echo "000")
    echo "Frontend (https://$CF/): HTTP $FRONTEND_STATUS"
    echo "API health:              HTTP $HEALTH_STATUS"
    [ "$FRONTEND_STATUS" = "200" ] && ok "Frontend OK" || warn "Frontend returned $FRONTEND_STATUS"
    [ "$HEALTH_STATUS" = "200" ] && ok "API health OK" || warn "API health returned $HEALTH_STATUS"
  else
    warn "CloudFront domain not found in SSM — cannot verify"
  fi

  if [ "$SKIP_AGENTS" = false ]; then
    echo ""
    export KUBECONFIG="/tmp/adp-deploy-kubeconfig"
    aws eks update-kubeconfig --name "adp-${ENVIRONMENT}-eks-cluster" --region "$AWS_REGION" --kubeconfig "$KUBECONFIG" 2>/dev/null || true
    GATEWAY_PODS=$(kubectl get pods -n adp-gateway -l app=bedrockgateway --no-headers 2>/dev/null | grep -c Running || echo "0")
    echo "Gateway pods running: $GATEWAY_PODS"
    KEDA_READY=$(kubectl get scaledjobs -n adp-agents --no-headers 2>/dev/null | head -1 || echo "not found")
    echo "KEDA ScaledJob: $KEDA_READY"
  fi
fi

# =============================================================================
# Summary
# =============================================================================
step "Deployment Complete"

echo "Platform URL: https://${CF:-<pending>}/"
echo ""

# Print credentials
CREDS=$(aws secretsmanager get-secret-value --secret-id "adp/${ENVIRONMENT}/gateway/test-admin-credentials" \
  --query SecretString --output text --region "$AWS_REGION" 2>/dev/null || echo "")
if [ -n "$CREDS" ]; then
  ADMIN_EMAIL=$(echo "$CREDS" | python3 -c "import sys,json;print(json.load(sys.stdin).get('username',''))" 2>/dev/null)
  ADMIN_PASS=$(echo "$CREDS" | python3 -c "import sys,json;print(json.load(sys.stdin).get('password',''))" 2>/dev/null)
  echo "Login credentials:"
  echo "  Email:    $ADMIN_EMAIL"
  echo "  Password: $ADMIN_PASS"
fi

if [ "$SKIP_AGENTS" = false ]; then
  echo ""
  WEBHOOK_API=$(aws apigateway get-rest-apis --region "$AWS_REGION" \
    --query 'items[?contains(name,`webhook-ingress`)].id' --output text 2>/dev/null || echo "")
  if [ -n "$WEBHOOK_API" ]; then
    echo "Webhook URL: https://${WEBHOOK_API}.execute-api.${AWS_REGION}.amazonaws.com/${ENVIRONMENT}/github"
  fi
  echo ""
  echo "To complete setup, log in as platform_admin and go to:"
  echo "  Settings → Connections → 'Set up GitHub App'"
fi

# Copy kubeconfig to the standard location so kubectl works without KUBECONFIG
# being set in subsequent interactive shells (e.g. SSH into the EC2 instance).
if [ -f /tmp/adp-deploy-kubeconfig ]; then
  KUBE_HOME="${HOME:-/root}"
  mkdir -p "${KUBE_HOME}/.kube"
  cp /tmp/adp-deploy-kubeconfig "${KUBE_HOME}/.kube/config"
  chmod 600 "${KUBE_HOME}/.kube/config"
fi

DEPLOY_END=$(date +%s)
ELAPSED=$(( DEPLOY_END - DEPLOY_START ))
echo ""
echo "Deploy finished at: $(date -u '+%Y-%m-%d %H:%M:%S UTC') (elapsed: $((ELAPSED / 60))m $((ELAPSED % 60))s)"
echo "============================================================================="
