#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# ADP — Wire the registered GitHub App into the running platform
# =============================================================================
# After the GitHub App ("adp-agent-platform") has been created and its
# credentials stored in Secrets Manager (by register-github-app.sh, or by the
# manual create flow), this script wires it into the *running* services so that:
#
#   1. The webhook agent path works  — the github-webhook Lambda already reads
#      adp/<env>/github-app/adp-agent-platform-{id,key} at runtime (no action
#      needed here), and the webhook secret is already set.
#
#   2. GitHub LOGIN works            — the auth-broker Lambda needs the OAuth
#      client_id (env) + client_secret (in adp/<env>/cognito/github-oauth-
#      credentials). A GitHub App has its OWN client_id/secret, so we reuse the
#      app's — no separate OAuth App required.
#
#   3. The UI "Link GitHub" install flow points at THIS app — the gateway reads
#      BG_GITHUB_APP_SLUG / BG_GITHUB_APP_ID / BG_GITHUB_APP_PRIVATE_KEY. Without
#      them it falls back to a hardcoded default slug (wrong app).
#
# This is the repeatable, NON-INTERACTIVE companion to register-github-app.sh.
# Everything here is idempotent and CLI-only EXCEPT obtaining the OAuth client
# secret, which GitHub shows only once at generation time — pass it via
# --client-secret or the GH_APP_CLIENT_SECRET env var. If omitted, login wiring
# is skipped (with a warning) and the rest still runs.
#
# Usage:
#   ./wire-github-app.sh --app-slug adp-agent-platform [options]
#
# Options / env (flags win over env):
#   --app-slug SLUG          GitHub App slug (e.g. adp-agent-platform).        [required]
#   --env ENV                Environment (default: dev).                       [ADP_ENV]
#   --client-id ID           OAuth client_id of the app (e.g. Iv23...). If
#                            omitted, auto-fetched from the GitHub API via gh.   [GH_APP_CLIENT_ID]
#   --client-secret SECRET   OAuth client secret (shown once at generation).
#                            If omitted, GitHub login wiring is skipped.         [GH_APP_CLIENT_SECRET]
#   --region REGION          AWS region (default: us-east-1).                    [AWS_REGION]
#   --namespace NS           Gateway k8s namespace (default: adp-gateway).
#   --skip-gateway           Don't touch the gateway configmap/secret/rollout.
#   --skip-login             Don't wire the OAuth login (broker) path.
# =============================================================================

APP_SLUG=""
ENVIRONMENT="${ADP_ENV:-dev}"
CLIENT_ID="${GH_APP_CLIENT_ID:-}"
CLIENT_SECRET="${GH_APP_CLIENT_SECRET:-}"
AWS_REGION="${AWS_REGION:-us-east-1}"
GW_NAMESPACE="adp-gateway"
SKIP_GATEWAY=false
SKIP_LOGIN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-slug)       APP_SLUG="$2"; shift 2 ;;
    --env)            ENVIRONMENT="$2"; shift 2 ;;
    --client-id)      CLIENT_ID="$2"; shift 2 ;;
    --client-secret)  CLIENT_SECRET="$2"; shift 2 ;;
    --region)         AWS_REGION="$2"; shift 2 ;;
    --namespace)      GW_NAMESPACE="$2"; shift 2 ;;
    --skip-gateway)   SKIP_GATEWAY=true; shift ;;
    --skip-login)     SKIP_LOGIN=true; shift ;;
    -h|--help)        sed -n '2,55p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}⚠${NC} $1"; }
fail() { echo -e "${RED}✗${NC} $1"; exit 1; }
info() { echo -e "${BLUE}ℹ${NC} $1"; }

[ -n "$APP_SLUG" ] || fail "--app-slug is required (e.g. adp-agent-platform)"
command -v aws &>/dev/null || fail "AWS CLI not installed"

ID_SECRET="adp/${ENVIRONMENT}/github-app/${APP_SLUG}-id"
KEY_SECRET="adp/${ENVIRONMENT}/github-app/${APP_SLUG}-key"
OAUTH_SECRET="adp/${ENVIRONMENT}/cognito/github-oauth-credentials"
BROKER_FN="bedrockgw-${ENVIRONMENT}-github-auth-broker"

echo ""
echo "Wiring GitHub App into ADP — slug=$APP_SLUG env=$ENVIRONMENT region=$AWS_REGION"
echo ""

# -----------------------------------------------------------------------------
# 0. Sanity: the app credentials must already be in Secrets Manager.
# -----------------------------------------------------------------------------
APP_ID=$(aws secretsmanager get-secret-value --secret-id "$ID_SECRET" \
  --query SecretString --output text --region "$AWS_REGION" 2>/dev/null || echo "")
KEY_LEN=$(aws secretsmanager get-secret-value --secret-id "$KEY_SECRET" \
  --query SecretString --output text --region "$AWS_REGION" 2>/dev/null | wc -c | tr -d ' ')
if [ -z "$APP_ID" ] || ! [[ "$APP_ID" =~ ^[0-9]+$ ]] || [ "${KEY_LEN:-0}" -lt 100 ]; then
  fail "App credentials not found / invalid in Secrets Manager ($ID_SECRET, $KEY_SECRET). Run register-github-app.sh (or the manual create flow) first."
fi
ok "App credentials present: App ID $APP_ID, key ${KEY_LEN} bytes"

# -----------------------------------------------------------------------------
# 1. Resolve OAuth client_id (for login). A GitHub App has its own client_id.
# -----------------------------------------------------------------------------
if [ -z "$CLIENT_ID" ] && command -v gh &>/dev/null; then
  CLIENT_ID=$(gh api "/apps/${APP_SLUG}" --jq '.client_id' 2>/dev/null || echo "")
  [ -n "$CLIENT_ID" ] && ok "Auto-fetched client_id from GitHub: $CLIENT_ID"
fi

# -----------------------------------------------------------------------------
# 2. GitHub LOGIN wiring (auth-broker)
# -----------------------------------------------------------------------------
if [ "$SKIP_LOGIN" = true ]; then
  warn "Skipping login wiring (--skip-login)."
elif [ -z "$CLIENT_SECRET" ]; then
  warn "No OAuth client secret provided (--client-secret / GH_APP_CLIENT_SECRET)."
  warn "  Generate one: App settings → 'Generate a new client secret' (shown once)."
  warn "  Skipping login wiring; re-run with --client-secret to enable GitHub login."
elif [ -z "$CLIENT_ID" ]; then
  warn "No client_id resolved (pass --client-id or install gh). Skipping login wiring."
else
  # 2a. Store client_id + client_secret JSON in the broker's secret.
  OAUTH_JSON=$(python3 -c 'import json,sys; print(json.dumps({"client_id":sys.argv[1],"client_secret":sys.argv[2]}))' "$CLIENT_ID" "$CLIENT_SECRET")
  if aws secretsmanager describe-secret --secret-id "$OAUTH_SECRET" --region "$AWS_REGION" &>/dev/null; then
    aws secretsmanager put-secret-value --secret-id "$OAUTH_SECRET" --secret-string "$OAUTH_JSON" --region "$AWS_REGION" >/dev/null
  else
    aws secretsmanager create-secret --name "$OAUTH_SECRET" --secret-string "$OAUTH_JSON" --region "$AWS_REGION" >/dev/null
  fi
  ok "Stored OAuth credentials in $OAUTH_SECRET (client_id=$CLIENT_ID)"

  # 2b. Resolve the OAuth callback URL the broker must advertise as redirect_uri
  #     (handler.py uses CALLBACK_URL). It's the gateway API GW invoke URL +
  #     /auth/github/callback. Terraform initializes broker CALLBACK_URL="" with
  #     ignore_changes, expecting it set post-deploy — so we set it here.
  #     This SAME value must be the app's "Callback URL" on GitHub.
  if [ -z "${CALLBACK_URL:-}" ]; then
    APIGW_URL=$(aws ssm get-parameter --name "/adp/${ENVIRONMENT}/gateway/apigw-invoke-url" \
      --query Parameter.Value --output text --region "$AWS_REGION" 2>/dev/null || echo "")
    [ -n "$APIGW_URL" ] && CALLBACK_URL="${APIGW_URL}/auth/github/callback"
  fi
  if [ -n "${CALLBACK_URL:-}" ]; then
    ok "OAuth callback URL: $CALLBACK_URL"
    warn "  Ensure the GitHub App's 'Callback URL' field is set to EXACTLY this value."
  else
    warn "Could not resolve callback URL (SSM /adp/${ENVIRONMENT}/gateway/apigw-invoke-url empty)."
    warn "  Login will fail until the broker CALLBACK_URL + the app's Callback URL are set."
  fi

  # 2c. The broker reads GITHUB_CLIENT_ID + CALLBACK_URL from its env (neither is
  #     secret) and the client-secret VALUE from Secrets Manager at runtime. Set
  #     the env + bump a refresh marker so the cached secret drops on next cold start.
  if aws lambda get-function --function-name "$BROKER_FN" --region "$AWS_REGION" >/dev/null 2>&1; then
    CUR_ENV=$(aws lambda get-function-configuration --function-name "$BROKER_FN" --region "$AWS_REGION" --query 'Environment.Variables' --output json 2>/dev/null || echo "{}")
    NEW_ENV=$(echo "$CUR_ENV" | CLIENT_ID="$CLIENT_ID" CB_URL="${CALLBACK_URL:-}" python3 -c '
import json,sys,os
e=json.load(sys.stdin) or {}
e["GITHUB_CLIENT_ID"]=os.environ["CLIENT_ID"]
cb=os.environ.get("CB_URL","")
if cb:
    e["CALLBACK_URL"]=cb
# bump a marker WITHOUT time-based nondeterminism issues — use the client_id prefix
e["OAUTH_SECRET_REFRESH_AT"]=os.environ["CLIENT_ID"][:12]
print(json.dumps({"Variables":e}))')
    aws lambda update-function-configuration --function-name "$BROKER_FN" \
      --environment "$NEW_ENV" --region "$AWS_REGION" >/dev/null \
      && ok "Broker Lambda env updated (GITHUB_CLIENT_ID${CALLBACK_URL:+ + CALLBACK_URL} set; secret picked up on next cold start)" \
      || warn "Could not update broker Lambda env (set GITHUB_CLIENT_ID=$CLIENT_ID manually)."
  else
    warn "Broker Lambda $BROKER_FN not found — is gateway-infra deployed? Secret stored; env not set."
  fi
fi

# -----------------------------------------------------------------------------
# 3. Gateway UI "Link GitHub" install flow — point it at THIS app.
# -----------------------------------------------------------------------------
if [ "$SKIP_GATEWAY" = true ]; then
  warn "Skipping gateway wiring (--skip-gateway)."
elif ! command -v kubectl &>/dev/null; then
  warn "kubectl not found — skipping gateway wiring. Set BG_GITHUB_APP_SLUG/ID/PRIVATE_KEY manually."
elif ! kubectl get namespace "$GW_NAMESPACE" &>/dev/null; then
  warn "Namespace $GW_NAMESPACE not found — gateway not deployed? Skipping gateway wiring."
else
  PRIVATE_KEY=$(aws secretsmanager get-secret-value --secret-id "$KEY_SECRET" --query SecretString --output text --region "$AWS_REGION")

  # 3a. Slug + App ID are non-secret → configmap. Patch idempotently.
  kubectl patch configmap bedrockgateway-config -n "$GW_NAMESPACE" --type merge -p "$(python3 -c '
import json,sys
print(json.dumps({"data":{
  "BG_GITHUB_APP_SLUG": sys.argv[1],
  "BG_GITHUB_APP_ID": sys.argv[2],
}}))' "$APP_SLUG" "$APP_ID")" >/dev/null
  ok "configmap: BG_GITHUB_APP_SLUG=$APP_SLUG, BG_GITHUB_APP_ID=$APP_ID"

  # 3b. Private key is sensitive → secret. Add key to bedrockgateway-secrets.
  kubectl patch secret bedrockgateway-secrets -n "$GW_NAMESPACE" --type merge -p "$(python3 -c '
import json,sys,base64
pk=open(sys.argv[1]).read() if False else sys.stdin.read()
print(json.dumps({"data":{"BG_GITHUB_APP_PRIVATE_KEY": base64.b64encode(pk.encode()).decode()}}))' /dev/stdin <<<"$PRIVATE_KEY")" >/dev/null
  ok "secret: BG_GITHUB_APP_PRIVATE_KEY set"

  # NOTE: deployment.yaml uses envFrom configMapRef for the configmap, but the
  # private key must be exposed as an env var from the secret. If the deployment
  # does not already map BG_GITHUB_APP_PRIVATE_KEY from bedrockgateway-secrets,
  # add it (see k8s/deployment.yaml). Warn so the operator can verify.
  if ! kubectl get deploy bedrockgateway -n "$GW_NAMESPACE" -o yaml | grep -q "BG_GITHUB_APP_PRIVATE_KEY"; then
    warn "deployment does not yet reference BG_GITHUB_APP_PRIVATE_KEY from the secret."
    warn "  Add an env entry mapping it from bedrockgateway-secrets, then it takes effect on rollout."
  fi

  # 3c. Restart so new config is picked up.
  kubectl rollout restart deployment/bedrockgateway -n "$GW_NAMESPACE" >/dev/null
  ok "Gateway rollout restarted to pick up new GitHub App config"
fi

echo ""
echo -e "${GREEN}=========================================${NC}"
echo -e "${GREEN}GitHub App wiring complete${NC}"
echo -e "${GREEN}=========================================${NC}"
echo "  App slug:   $APP_SLUG  (App ID $APP_ID)"
echo "  Webhook agent path: live (Lambda reads $ID_SECRET / $KEY_SECRET at runtime)"
[ "$SKIP_LOGIN" = false ] && [ -n "$CLIENT_SECRET" ] && [ -n "$CLIENT_ID" ] && echo "  GitHub login: wired (client_id=$CLIENT_ID)" || echo "  GitHub login: NOT wired (re-run with --client-secret)"
[ "$SKIP_GATEWAY" = false ] && echo "  UI 'Link GitHub' install: points at $APP_SLUG"
echo ""
echo "Next: install the app on a repo → https://github.com/apps/${APP_SLUG}/installations/new"
echo "Then @mention an agent in an issue/PR comment (e.g. @agent-developer ...)."
