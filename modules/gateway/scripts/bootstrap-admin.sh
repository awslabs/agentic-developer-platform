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
# The entrypoint defaults --org to "platform-admin" when omitted; mirror that
# here so step 4 sets the matching Cognito attribute.
EFFECTIVE_ORG="${ORG:-platform-admin}"
ORG_ARG=""
[ -n "$ORG" ] && ORG_ARG="--org $ORG"

CMD="kubectl exec deploy/${DEPLOY} -n ${NAMESPACE} -- \
  python -m src.admin.onboarding.bootstrap_admin \
    --cognito-sub '${SUB}' --email '${EMAIL}' --role '${ROLE}' ${ORG_ARG}"

echo ""
echo "Running in-pod bootstrap..."
run "$CMD"

# -----------------------------------------------------------------------------
# 4. Mirror role + org onto the Cognito user's custom attributes.
#    The pre-token-generation Lambda (handle_user_token_generation) copies
#    custom:role / custom:org_id from the USER ATTRIBUTES into the access token —
#    it does NOT read the DB. So seeding only the DB row leaves the operator
#    logged in with an empty org/role in the UI until these are set. Setting
#    them here makes a fresh login mint a token carrying org_id + role.
# -----------------------------------------------------------------------------
ATTR_CMD="aws cognito-idp admin-update-user-attributes \
  --user-pool-id '${POOL_ID}' --username '${EMAIL}' --region '${AWS_REGION}' \
  --user-attributes Name=custom:role,Value='${ROLE}' Name=custom:org_id,Value='${EFFECTIVE_ORG}'"

echo ""
echo "Setting Cognito custom:role + custom:org_id (read by the pre-token Lambda)..."
run "$ATTR_CMD"
ok "Cognito attributes set: custom:role=${ROLE} custom:org_id=${EFFECTIVE_ORG}"

# -----------------------------------------------------------------------------
# 5. Verify the Cognito attributes were actually written.
#    A partial run (DB only) must NOT pass silently — fail loudly if the
#    attributes are missing so operators know the admin UI will be broken.
# -----------------------------------------------------------------------------
echo ""
echo "Verifying Cognito attributes (read-back)..."
if [ "$DRY_RUN" = true ]; then
  echo -e "${BLUE}[dry-run]${NC} skipping attribute verification"
else
  ACTUAL_ROLE=$(aws cognito-idp admin-get-user \
    --user-pool-id "$POOL_ID" --username "$EMAIL" --region "$AWS_REGION" \
    --query 'UserAttributes[?Name==`custom:role`].Value' --output text 2>/dev/null || echo "")
  ACTUAL_ORG=$(aws cognito-idp admin-get-user \
    --user-pool-id "$POOL_ID" --username "$EMAIL" --region "$AWS_REGION" \
    --query 'UserAttributes[?Name==`custom:org_id`].Value' --output text 2>/dev/null || echo "")

  if [ -z "$ACTUAL_ROLE" ] || [ "$ACTUAL_ROLE" = "None" ]; then
    fail "VERIFICATION FAILED: custom:role is NOT set on Cognito user $EMAIL. The admin UI will not recognize this user as an admin. Run bootstrap-admin again or manually set the attribute with: aws cognito-idp admin-update-user-attributes --user-pool-id '$POOL_ID' --username '$EMAIL' --region '$AWS_REGION' --user-attributes Name=custom:role,Value='$ROLE'"
  fi
  if [ "$ACTUAL_ROLE" != "$ROLE" ]; then
    fail "VERIFICATION FAILED: custom:role is '$ACTUAL_ROLE' but expected '$ROLE'. Fix with: aws cognito-idp admin-update-user-attributes --user-pool-id '$POOL_ID' --username '$EMAIL' --region '$AWS_REGION' --user-attributes Name=custom:role,Value='$ROLE'"
  fi
  if [ -z "$ACTUAL_ORG" ] || [ "$ACTUAL_ORG" = "None" ]; then
    fail "VERIFICATION FAILED: custom:org_id is NOT set on Cognito user $EMAIL. The admin UI will show an empty org. Run bootstrap-admin again or manually set the attribute with: aws cognito-idp admin-update-user-attributes --user-pool-id '$POOL_ID' --username '$EMAIL' --region '$AWS_REGION' --user-attributes Name=custom:org_id,Value='$EFFECTIVE_ORG'"
  fi
  ok "Verified: custom:role=$ACTUAL_ROLE custom:org_id=$ACTUAL_ORG"
fi

echo ""
ok "Bootstrap complete (idempotent — 'already-bootstrapped' is success)."
echo "  Verify: log out + back in as $EMAIL (email/password) so a fresh token picks"
echo "          up custom:role/custom:org_id — access status should be 'registered',"
echo "          org_id=${EFFECTIVE_ORG}; then approve pending GitHub users from the admin UI."
