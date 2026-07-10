#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# platform-status.sh — Print full status of the deployed ADP platform
# =============================================================================
# Shows login URL, credentials, service endpoints, and health of all components.
#
# Usage:
#   export AWS_PROFILE=<profile> AWS_REGION=<region>
#   ./platform/scripts/platform-status.sh [--env dev] [--region us-east-1]
# =============================================================================

ENVIRONMENT="${ENVIRONMENT:-dev}"
AWS_REGION="${AWS_REGION:-us-east-1}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)    ENVIRONMENT="$2"; shift 2 ;;
    --region) AWS_REGION="$2"; shift 2 ;;
    -h|--help) echo "Usage: $0 [--env ENV] [--region REGION]"; exit 0 ;;
    *) shift ;;
  esac
done

export AWS_REGION AWS_PAGER=""

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
warn() { echo -e "  ${YELLOW}⚠${NC} $1"; }
err()  { echo -e "  ${RED}✗${NC} $1"; }
header() { echo -e "\n${BLUE}═══ $1 ═══${NC}"; }
label()  { printf "  ${CYAN}%-24s${NC} %s\n" "$1:" "$2"; }

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo "unknown")
GITHUB_APP_MISSING=false

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║           ADP Platform Status                               ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  Account:     $ACCOUNT_ID                          ║"
echo "║  Region:      $(printf '%-40s' "$AWS_REGION")║"
echo "║  Environment: $(printf '%-40s' "$ENVIRONMENT")║"
echo "╚══════════════════════════════════════════════════════════════╝"

# =============================================================================
# Access / Login
# =============================================================================
header "Access"

CF=$(aws ssm get-parameter --name "/adp/${ENVIRONMENT}/gateway/cloudfront-domain" \
  --query Parameter.Value --output text --region "$AWS_REGION" 2>/dev/null || echo "")

if [ -n "$CF" ] && [ "$CF" != "None" ]; then
  label "Platform URL" "https://$CF/"
  FRONTEND_HTTP=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "https://$CF/" 2>/dev/null || echo "000")
  [ "$FRONTEND_HTTP" = "200" ] && ok "Frontend reachable (HTTP $FRONTEND_HTTP)" || warn "Frontend returned HTTP $FRONTEND_HTTP"
else
  err "CloudFront domain not found in SSM — platform may not be deployed"
fi

CREDS=$(aws secretsmanager get-secret-value --secret-id "adp/${ENVIRONMENT}/gateway/test-admin-credentials" \
  --query SecretString --output text --region "$AWS_REGION" 2>/dev/null || echo "")
if [ -n "$CREDS" ] && [ "$CREDS" != "None" ]; then
  ADMIN_EMAIL=$(echo "$CREDS" | python3 -c "import sys,json;print(json.load(sys.stdin).get('username',''))" 2>/dev/null || echo "")
  ADMIN_PASS=$(echo "$CREDS" | python3 -c "import sys,json;print(json.load(sys.stdin).get('password',''))" 2>/dev/null || echo "")
  label "Admin Email" "$ADMIN_EMAIL"
  label "Admin Password" "$ADMIN_PASS"
else
  warn "Admin credentials secret not found"
fi

# =============================================================================
# API Health
# =============================================================================
header "API"

if [ -n "$CF" ] && [ "$CF" != "None" ]; then
  HEALTH_HTTP=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "https://$CF/api/health" 2>/dev/null || echo "000")
  HEALTH_BODY=$(curl -s --connect-timeout 5 "https://$CF/api/health" 2>/dev/null || echo "")
  label "Health endpoint" "https://$CF/api/health"
  [ "$HEALTH_HTTP" = "200" ] && ok "API healthy ($HEALTH_BODY)" || err "API returned HTTP $HEALTH_HTTP"
fi

API_ID=$(aws apigateway get-rest-apis --region "$AWS_REGION" \
  --query "items[?contains(name,'bedrockgw-${ENVIRONMENT}')].{id:id,name:name}" --output text 2>/dev/null | head -1 || echo "")
if [ -n "$API_ID" ]; then
  label "Gateway API ID" "$(echo "$API_ID" | awk '{print $1}')"
  label "Gateway API URL" "https://$(echo "$API_ID" | awk '{print $1}').execute-api.${AWS_REGION}.amazonaws.com/${ENVIRONMENT}"
fi

# =============================================================================
# EKS / Kubernetes
# =============================================================================
header "EKS Cluster"

EKS_STATUS=$(aws eks describe-cluster --name "adp-${ENVIRONMENT}-eks-cluster" --region "$AWS_REGION" \
  --query 'cluster.status' --output text 2>/dev/null || echo "NOT_FOUND")
label "Cluster" "adp-${ENVIRONMENT}-eks-cluster"
label "Status" "$EKS_STATUS"

if [ "$EKS_STATUS" = "ACTIVE" ]; then
  EKS_VERSION=$(aws eks describe-cluster --name "adp-${ENVIRONMENT}-eks-cluster" --region "$AWS_REGION" \
    --query 'cluster.version' --output text 2>/dev/null || echo "")
  label "Version" "$EKS_VERSION"

  # Try to get pod status (may fail if kubeconfig not set or IP not allowed)
  aws eks update-kubeconfig --name "adp-${ENVIRONMENT}-eks-cluster" --region "$AWS_REGION" 2>/dev/null || true

  echo ""
  echo -e "  ${CYAN}Gateway Pods:${NC}"
  if kubectl get pods -n adp-gateway -l app=bedrockgateway --no-headers 2>/dev/null | head -5; then
    :
  else
    warn "Cannot reach EKS API (IP may not be in public access CIDRs)"
  fi

  echo ""
  echo -e "  ${CYAN}Agent Pods:${NC}"
  kubectl get pods -n adp-agents --no-headers 2>/dev/null | head -10 || true

  echo ""
  echo -e "  ${CYAN}KEDA ScaledJob:${NC}"
  kubectl get scaledjobs -n adp-agents --no-headers 2>/dev/null || true

  echo ""
  echo -e "  ${CYAN}Nodes:${NC}"
  kubectl get nodes --no-headers 2>/dev/null | head -5 || true
fi

# =============================================================================
# Database (RDS)
# =============================================================================
header "Database (RDS)"

RDS_STATUS=$(aws rds describe-db-instances --region "$AWS_REGION" \
  --query 'DBInstances[?starts_with(DBInstanceIdentifier,`bedrockgw`)].{Id:DBInstanceIdentifier,Status:DBInstanceStatus,Engine:Engine,Class:DBInstanceClass}' \
  --output json 2>/dev/null || echo "[]")
if [ "$RDS_STATUS" != "[]" ]; then
  echo "$RDS_STATUS" | python3 -c "
import sys, json
for db in json.load(sys.stdin):
    print(f'  Instance:  {db[\"Id\"]}')
    print(f'  Status:    {db[\"Status\"]}')
    print(f'  Engine:    {db[\"Engine\"]}')
    print(f'  Class:     {db[\"Class\"]}')
" 2>/dev/null
else
  warn "No RDS instances found"
fi

# =============================================================================
# Redis (ElastiCache)
# =============================================================================
header "Redis (ElastiCache)"

REDIS=$(aws elasticache describe-replication-groups --region "$AWS_REGION" \
  --query 'ReplicationGroups[?contains(ReplicationGroupId,`bedrockgw`)].{Id:ReplicationGroupId,Status:Status}' \
  --output json 2>/dev/null || echo "[]")
if [ "$REDIS" != "[]" ]; then
  echo "$REDIS" | python3 -c "
import sys, json
for r in json.load(sys.stdin):
    print(f'  Cluster:   {r[\"Id\"]}')
    print(f'  Status:    {r[\"Status\"]}')
" 2>/dev/null
else
  warn "No Redis clusters found"
fi

# =============================================================================
# Cognito
# =============================================================================
header "Cognito"

POOL_ID=$(aws ssm get-parameter --name "/adp/${ENVIRONMENT}/gateway/cognito-user-pool-id" \
  --query Parameter.Value --output text --region "$AWS_REGION" 2>/dev/null || echo "")
if [ -n "$POOL_ID" ] && [ "$POOL_ID" != "None" ]; then
  label "User Pool ID" "$POOL_ID"
  USER_COUNT=$(aws cognito-idp list-users --user-pool-id "$POOL_ID" --region "$AWS_REGION" \
    --query 'length(Users)' --output text 2>/dev/null || echo "?")
  label "Users" "$USER_COUNT"
else
  warn "Cognito pool not found"
fi

# =============================================================================
# CloudFront
# =============================================================================
header "CloudFront"

CF_ID=$(aws ssm get-parameter --name "/adp/${ENVIRONMENT}/gateway/cloudfront-id" \
  --query Parameter.Value --output text --region "$AWS_REGION" 2>/dev/null || echo "")
if [ -n "$CF_ID" ] && [ "$CF_ID" != "None" ]; then
  CF_STATUS=$(aws cloudfront get-distribution --id "$CF_ID" \
    --query 'Distribution.Status' --output text 2>/dev/null || echo "unknown")
  label "Distribution ID" "$CF_ID"
  label "Status" "$CF_STATUS"
  label "Domain" "$CF"
else
  warn "CloudFront distribution not found"
fi

# =============================================================================
# Webhook Agent Stack
# =============================================================================
header "Webhook Agent Stack"

WEBHOOK_API=$(aws apigateway get-rest-apis --region "$AWS_REGION" \
  --query "items[?contains(name,'webhook-ingress')].id" --output text 2>/dev/null || echo "")
if [ -n "$WEBHOOK_API" ]; then
  WEBHOOK_URL="https://${WEBHOOK_API}.execute-api.${AWS_REGION}.amazonaws.com/${ENVIRONMENT}/github"
  label "Webhook URL" "$WEBHOOK_URL"

  # Test webhook endpoint (unsigned POST should return 401 = working)
  WH_HTTP=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 -X POST "$WEBHOOK_URL" -d '{}' 2>/dev/null || echo "000")
  if [ "$WH_HTTP" = "401" ] || [ "$WH_HTTP" = "403" ]; then
    ok "Webhook endpoint live (returns $WH_HTTP for unsigned request)"
  elif [ "$WH_HTTP" = "502" ]; then
    warn "Webhook returns 502 (Lambda may need warming)"
  else
    err "Webhook returned unexpected HTTP $WH_HTTP"
  fi
else
  warn "Webhook ingress API not found — agent stack may not be deployed"
fi

# SQS queue
SQS_URL=$(aws sqs list-queues --region "$AWS_REGION" --queue-name-prefix "adp-${ENVIRONMENT}-agent-submit" \
  --query 'QueueUrls[0]' --output text 2>/dev/null || echo "")
if [ -n "$SQS_URL" ] && [ "$SQS_URL" != "None" ]; then
  MSG_COUNT=$(aws sqs get-queue-attributes --queue-url "$SQS_URL" --region "$AWS_REGION" \
    --attribute-names ApproximateNumberOfMessages --query 'Attributes.ApproximateNumberOfMessages' --output text 2>/dev/null || echo "?")
  label "SQS Queue" "$(basename "$SQS_URL")"
  label "Messages pending" "$MSG_COUNT"
fi

# DynamoDB tables
echo ""
echo -e "  ${CYAN}DynamoDB tables:${NC}"
for table in tenant-registry webhook-events rate-limits identity-index; do
  STATUS=$(aws dynamodb describe-table --table-name "adp-${ENVIRONMENT}-${table}" --region "$AWS_REGION" \
    --query 'Table.TableStatus' --output text 2>/dev/null || echo "NOT_FOUND")
  if [ "$STATUS" = "ACTIVE" ]; then
    ok "adp-${ENVIRONMENT}-${table}: $STATUS"
  elif [ "$STATUS" = "NOT_FOUND" ]; then
    :  # skip — not deployed
  else
    warn "adp-${ENVIRONMENT}-${table}: $STATUS"
  fi
done

# =============================================================================
# Bedrock Model Access
# =============================================================================
header "Bedrock"

BEDROCK_OK=$(aws bedrock-runtime invoke-model --model-id us.anthropic.claude-opus-4-6-v1 \
  --body '{"anthropic_version":"bedrock-2023-05-31","max_tokens":8,"messages":[{"role":"user","content":"hi"}]}' \
  --cli-binary-format raw-in-base64-out /tmp/bedrock-test-out.json --region "$AWS_REGION" >/dev/null 2>&1 && echo "yes" || echo "no")
rm -f /tmp/bedrock-test-out.json 2>/dev/null
if [ "$BEDROCK_OK" = "yes" ]; then
  ok "Claude Opus 4.6 accessible (us.anthropic.claude-opus-4-6-v1)"
else
  err "Claude Opus 4.6 NOT accessible — agents will hang silently"
fi

# =============================================================================
# GitHub App
# =============================================================================
header "GitHub App"

APP_ID=$(aws secretsmanager get-secret-value --secret-id "adp/${ENVIRONMENT}/github-app/adp-agent-platform-id" \
  --query SecretString --output text --region "$AWS_REGION" 2>/dev/null || echo "")
if [ -n "$APP_ID" ] && [ "$APP_ID" != "None" ]; then
  label "App ID" "$APP_ID"
  WEBHOOK_SECRET=$(aws secretsmanager get-secret-value --secret-id "adp/${ENVIRONMENT}/webhook-ingress/github-webhook-secret" \
    --query SecretString --output text --region "$AWS_REGION" 2>/dev/null || echo "")
  if [ -n "$WEBHOOK_SECRET" ]; then
    label "Webhook Secret" "${WEBHOOK_SECRET:0:8}... (${#WEBHOOK_SECRET} chars)"
  fi
  if [ -n "$WEBHOOK_API" ]; then
    echo ""
    echo -e "  ${CYAN}GitHub App Settings:${NC}"
    echo "    Webhook URL:    $WEBHOOK_URL"
    echo "    Webhook Secret: $WEBHOOK_SECRET"
  fi
else
  warn "GitHub App not registered — set up via UI: Settings → Connections → 'Set up GitHub App'"
  GITHUB_APP_MISSING=true
fi

# =============================================================================
# S3 Buckets
# =============================================================================
header "S3 Buckets"

for bucket in $(aws s3 ls 2>/dev/null | awk '{print $3}' | grep -E "adp-|bedrockgw-" || true); do
  label "Bucket" "$bucket"
done

# =============================================================================
# Cost estimate (idle)
# =============================================================================
header "Estimated Idle Cost"

echo "  RDS (db.t3.medium):     ~\$30/mo"
echo "  ElastiCache (t3.micro): ~\$15/mo"
echo "  EKS cluster:            ~\$73/mo"
echo "  NAT Gateway:            ~\$32/mo"
echo "  CloudFront:             ~\$1/mo"
echo "  Other (Lambda, DDB):    ~\$5/mo"
echo "  ─────────────────────────────────"
echo "  Total (approx):         ~\$156/mo"

echo ""
if [ "$GITHUB_APP_MISSING" = true ]; then
  echo "═══════════════════════════════════════════════════════════════"
  echo ""
  echo "  To register the GitHub App (required for agents):"
  echo "    Log in as platform_admin → Settings → Connections → 'Set up GitHub App'"
  echo ""
fi
echo "═══════════════════════════════════════════════════════════════"
