#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# bootstrap-admin.sh — Seed the first platform admin's DB rows
# =============================================================================
# A fresh gateway deploy has no rows in the `users` table, so the onboarding
# gate returns "request access" for EVERYONE — including the seeded Cognito
# admin — and nobody can approve anyone (dead-end). create_test_users=true only
# creates the Cognito user, not the DB rows.
#
# This resolves the seeded admin's Cognito sub and runs the in-pod bootstrap
# entrypoint (which reuses the gateway's DB IAM access) to seed the org/tenant/
# dept/team/user/cognito-identity. Idempotent. Run AFTER the gateway backend is
# deployed and healthy.
#
# Usage:
#   ./bootstrap-admin.sh [--env dev] [--region us-east-1] [--dry-run]
#                        [--email <addr>] [--pool-id <id>] [--org <slug>]
#                        [--role <platform_admin|org_admin>]
#                        [--namespace adp-gateway] [--deploy bedrockgateway]
#
# Defaults: email + pool-id are read from adp/<env>/gateway/test-admin-credentials.
#           org defaults to "platform-admin" (the entrypoint's default).
# =============================================================================

ENVIRONMENT="${ADP_ENV:-dev}"
AWS_REGION="${AWS_REGION:-us-east-1}"
DRY_RUN=false
EMAIL=""
POOL_ID=""
ORG=""
ROLE="platform_admin"
NAMESPACE="adp-gateway"
DEPLOY="bedrockgateway"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)        ENVIRONMENT="$2"; shift 2 ;;
    --region)     AWS_REGION="$2"; shift 2 ;;
    --dry-run)    DRY_RUN=true; shift ;;
    --email)      EMAIL="$2"; shift 2 ;;
    --pool-id)    POOL_ID="$2"; shift 2 ;;
    --org)        ORG="$2"; shift 2 ;;
    --role)       ROLE="$2"; shift 2 ;;
    --namespace)  NAMESPACE="$2"; shift 2 ;;
    --deploy)     DEPLOY="$2"; shift 2 ;;
    -h|--help)    sed -n '4,30p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}⚠${NC} $1"; }
fail() { echo -e "${RED}✗${NC} $1"; exit 1; }
run()  { if [ "$DRY_RUN" = true ]; then echo -e "${BLUE}[dry-run]${NC} $*"; else eval "$@"; fi; }

command -v aws &>/dev/null     || fail "AWS CLI not installed"
command -v kubectl &>/dev/null || fail "kubectl not installed"
command -v python3 &>/dev/null || true   # only used for JSON parse of the secret

SECRET="adp/${ENVIRONMENT}/gateway/test-admin-credentials"

echo ""
echo "bootstrap-admin: env=$ENVIRONMENT region=$AWS_REGION ns=$NAMESPACE deploy=$DEPLOY"
echo ""

# -----------------------------------------------------------------------------
# 1. Resolve admin email + Cognito pool id (from the test-admin secret unless
#    overridden — e.g. an operator using their own SSO admin).
# -----------------------------------------------------------------------------
if [ -z "$EMAIL" ] || [ -z "$POOL_ID" ]; then
  SECRET_JSON=$(aws secretsmanager get-secret-value --secret-id "$SECRET" \
    --query SecretString --output text --region "$AWS_REGION" 2>/dev/null || echo "")
  if [ -z "$SECRET_JSON" ]; then
    fail "Could not read $SECRET. Pass --email and --pool-id explicitly (e.g. for an SSO admin)."
  fi
  [ -z "$EMAIL" ]   && EMAIL=$(echo "$SECRET_JSON"   | python3 -c 'import sys,json;print(json.load(sys.stdin).get("username",""))')
  [ -z "$POOL_ID" ] && POOL_ID=$(echo "$SECRET_JSON" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("cognito_user_pool_id",""))')
fi
[ -n "$EMAIL" ]   || fail "Admin email not resolved (pass --email)."
[ -n "$POOL_ID" ] || fail "Cognito pool id not resolved (pass --pool-id)."
ok "Admin: $EMAIL  (pool $POOL_ID)"

# -----------------------------------------------------------------------------
# 2. Resolve the admin's Cognito sub.
# -----------------------------------------------------------------------------
SUB=$(aws cognito-idp admin-get-user --user-pool-id "$POOL_ID" --username "$EMAIL" \
  --query 'UserAttributes[?Name==`sub`].Value' --output text --region "$AWS_REGION" 2>/dev/null || echo "")
[ -n "$SUB" ] || fail "Could not resolve Cognito sub for $EMAIL in pool $POOL_ID."
ok "Cognito sub: $SUB"

# -----------------------------------------------------------------------------
# 3. Run the in-pod bootstrap entrypoint (reuses the pod's DB IAM access).
# -----------------------------------------------------------------------------
ORG_ARG=""
[ -n "$ORG" ] && ORG_ARG="--org $ORG"

CMD="kubectl exec deploy/${DEPLOY} -n ${NAMESPACE} -- \
  python -m src.admin.onboarding.bootstrap_admin \
    --cognito-sub '${SUB}' --email '${EMAIL}' --role '${ROLE}' ${ORG_ARG}"

echo ""
echo "Running in-pod bootstrap..."
run "$CMD"

echo ""
ok "Bootstrap complete (idempotent — 'already-bootstrapped' is success)."
echo "  Verify: log in as $EMAIL (email/password) — access status should be 'registered',"
echo "          then approve any pending GitHub users from the admin UI."
