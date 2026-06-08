#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# deploy-frontend.sh — Phase 6: build + publish the gateway React frontend
# =============================================================================
# The manual, repeatable equivalent of gateway-deploy.yml's frontend job. A
# fresh self-managed deploy never fires that workflow, so this script:
#   1. builds the SPA with the FULL VITE_* env resolved from SSM (NOT just
#      VITE_API_URL — those vars are baked in at build time; omitting them ships
#      a broken app: "GitHub sign-in is not configured" + Cognito unconfigured),
#   2. syncs dist/ to the frontend bucket, EXCLUDING cfn-templates/* so the
#      --delete doesn't wipe the template uploaded next,
#   3. uploads the CloudFormation role template (cfn-templates/aws_role_v1.yaml)
#      — REQUIRED for the "Add AWS account" flow; the gateway pre-signs a GET for
#      it, and without it the flow fails "S3 error: The specified key does not
#      exist." (The paired gateway-infra grant — s3:ListBucket/GetObject on
#      cfn-templates/* — must also be applied; see modules/gateway/infra.)
#   4. invalidates CloudFront.
#
# Run AFTER gateway-infra (Phase 4) created the bucket + SSM params, and after
# the backend (Phase 5) is up. Idempotent.
#
# Usage:
#   ./deploy-frontend.sh [--env dev] [--region us-east-1] [--dry-run]
#                        [--skip-build] [--skip-template]
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)" # modules/gateway
FRONTEND_DIR="${MODULE_ROOT}/frontend"
CFN_TEMPLATE="${MODULE_ROOT}/src/auth/cfn_templates/aws_role_v1.yaml"

ENVIRONMENT="${ADP_ENV:-dev}"
AWS_REGION="${AWS_REGION:-us-east-1}"
DRY_RUN=false
SKIP_BUILD=false
SKIP_TEMPLATE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)           ENVIRONMENT="$2"; shift 2 ;;
    --region)        AWS_REGION="$2"; shift 2 ;;
    --dry-run)       DRY_RUN=true; shift ;;
    --skip-build)    SKIP_BUILD=true; shift ;;
    --skip-template) SKIP_TEMPLATE=true; shift ;;
    -h|--help)       sed -n '4,27p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}⚠${NC} $1"; }
fail() { echo -e "${RED}✗${NC} $1"; exit 1; }
run()  { if [ "$DRY_RUN" = true ]; then echo -e "${BLUE}[dry-run]${NC} $*"; else eval "$@"; fi; }

command -v aws &>/dev/null || fail "AWS CLI not installed"
command -v npm &>/dev/null || fail "npm not installed (Node >= 22)"
[ -d "$FRONTEND_DIR" ] || fail "Frontend dir not found: $FRONTEND_DIR"
[ -f "$CFN_TEMPLATE" ] || fail "CFN template not found: $CFN_TEMPLATE"

_ssm() { aws ssm get-parameter --name "$1" --query Parameter.Value --output text --region "$AWS_REGION" 2>/dev/null || echo ""; }

echo ""
echo "deploy-frontend: env=$ENVIRONMENT region=$AWS_REGION"
[ "$DRY_RUN" = true ] && warn "DRY RUN — no changes will be made"

# -----------------------------------------------------------------------------
# Resolve targets + VITE_* env from SSM (created by gateway-infra, Phase 4).
# -----------------------------------------------------------------------------
BUCKET=$(_ssm "/adp/${ENVIRONMENT}/gateway/frontend-bucket")
DIST_ID=$(_ssm "/adp/${ENVIRONMENT}/gateway/cloudfront-id")
[ -n "$BUCKET" ] || fail "frontend-bucket not in SSM — run Phase 4 (gateway-infra) first."
ok "Frontend bucket: $BUCKET"

# -----------------------------------------------------------------------------
# 1. Build with the full VITE_* env (mirrors gateway-deploy.yml).
# -----------------------------------------------------------------------------
if [ "$SKIP_BUILD" = true ]; then
  warn "Skipping build (--skip-build); expecting an existing dist/."
  [ -d "${FRONTEND_DIR}/dist" ] || fail "No dist/ to deploy and --skip-build set."
else
  POOL_ID=$(_ssm "/adp/${ENVIRONMENT}/gateway/cognito-user-pool-id")
  CLIENT_ID=$(_ssm "/adp/${ENVIRONMENT}/gateway/cognito-client-id")
  DOMAIN=$(_ssm "/adp/${ENVIRONMENT}/gateway/cognito-domain")
  BROKER=$(_ssm "/adp/${ENVIRONMENT}/gateway/github-auth-broker-url")
  WS_URL=$(_ssm "/adp/${ENVIRONMENT}/gateway/agent-ws-url")
  for v in POOL_ID CLIENT_ID DOMAIN; do
    [ -n "${!v}" ] || warn "VITE source $v is empty — login may be misconfigured (check gateway-infra)."
  done
  echo "Building frontend with VITE_* from SSM..."
  run "(cd '${FRONTEND_DIR}' && npm ci && \
    VITE_API_URL='/api' \
    VITE_COGNITO_REGION='${AWS_REGION}' \
    VITE_COGNITO_USER_POOL_ID='${POOL_ID}' \
    VITE_COGNITO_CLIENT_ID='${CLIENT_ID}' \
    VITE_COGNITO_DOMAIN='${DOMAIN}' \
    VITE_GITHUB_AUTH_BROKER_URL='${BROKER}' \
    VITE_AGENT_WS_URL='${WS_URL}' \
    npm run build)"
  ok "Frontend built"
fi

# -----------------------------------------------------------------------------
# 2. Sync dist/ to S3 (exclude cfn-templates/* so --delete keeps the template).
# -----------------------------------------------------------------------------
run "aws s3 sync '${FRONTEND_DIR}/dist/' 's3://${BUCKET}/' --delete --exclude 'cfn-templates/*' --region '${AWS_REGION}'"
ok "Synced dist/ → s3://${BUCKET}/"

# -----------------------------------------------------------------------------
# 3. Upload the CFN role template (required for "Add AWS account").
# -----------------------------------------------------------------------------
if [ "$SKIP_TEMPLATE" = true ]; then
  warn "Skipping CFN template upload (--skip-template) — Add-AWS-account will fail without it."
else
  run "aws s3 cp '${CFN_TEMPLATE}' 's3://${BUCKET}/cfn-templates/aws_role_v1.yaml' --content-type text/yaml --region '${AWS_REGION}'"
  ok "Uploaded cfn-templates/aws_role_v1.yaml"
fi

# -----------------------------------------------------------------------------
# 4. Invalidate CloudFront.
# -----------------------------------------------------------------------------
if [ -n "$DIST_ID" ] && [ "$DIST_ID" != "None" ]; then
  run "aws cloudfront create-invalidation --distribution-id '${DIST_ID}' --paths '/*' >/dev/null"
  ok "CloudFront invalidation created ($DIST_ID)"
else
  warn "cloudfront-id not in SSM — skipped invalidation."
fi

echo ""
ok "Frontend deploy complete."
echo "  Verify: curl -s -o /dev/null -w '%{http_code}\\n' \"https://\$(aws ssm get-parameter --name /adp/${ENVIRONMENT}/gateway/cloudfront-domain --query Parameter.Value --output text)/\"  # 200"
