#!/bin/bash
# =============================================================================
# ADP Preflight Check
# =============================================================================
# Validates that the deployer has all required tools, permissions, and
# configuration before starting deployment.
#
# Usage:
#   ./platform/scripts/preflight-check.sh              # Check for default (AWS-only) deploy
#   ./platform/scripts/preflight-check.sh --local      # Check for local deploy (needs Docker, Node, etc.)
#   ./platform/scripts/preflight-check.sh --full       # Check everything
# =============================================================================

set -euo pipefail

LOCAL_MODE=false
FULL_MODE=false
for arg in "$@"; do
  case $arg in
    --local) LOCAL_MODE=true ;;
    --full) FULL_MODE=true; LOCAL_MODE=true ;;
  esac
done

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
PASS=0; WARN=0; FAIL=0

pass() { echo -e "  ${GREEN}✓${NC} $1"; PASS=$((PASS+1)); }
warn() { echo -e "  ${YELLOW}⚠${NC} $1"; WARN=$((WARN+1)); }
fail() { echo -e "  ${RED}✗${NC} $1"; FAIL=$((FAIL+1)); }
section() { echo -e "\n${BLUE}── $1 ──${NC}"; }

# Load deployment config — populates ADP_ACCOUNT_ID (expected target account)
# and friends. Used below to detect operator-vs-config mismatches.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=load-deploy-config.sh
source "${SCRIPT_DIR}/load-deploy-config.sh"

echo ""
echo "ADP Preflight Check"
echo "==================="

# =============================================================================
# 1. Required CLI tools
# =============================================================================
section "CLI Tools — Deploy"

# AWS CLI (always required)
if command -v aws &>/dev/null; then
  AWS_VERSION=$(aws --version 2>&1 | head -1)
  pass "AWS CLI: $AWS_VERSION"
else
  fail "AWS CLI not installed. Install: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
fi

# zip (needed for CodeBuild source packaging in default mode)
if command -v zip &>/dev/null; then
  pass "zip: available"
elif [ "$LOCAL_MODE" = false ]; then
  fail "zip not installed (needed to package source for CodeBuild)"
else
  warn "zip not installed (only needed for default AWS deploy)"
fi

# Terraform (only for --local)
if command -v terraform &>/dev/null; then
  TF_VERSION=$(terraform version -json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['terraform_version'])" 2>/dev/null || terraform version | head -1)
  pass "Terraform: $TF_VERSION"
elif [ "$LOCAL_MODE" = true ]; then
  fail "Terraform not installed (required for --local). Install: https://developer.hashicorp.com/terraform/install"
fi

# Docker (only for --local)
if command -v docker &>/dev/null; then
  if docker info &>/dev/null; then
    pass "Docker: $(docker --version) (daemon running)"
  elif [ "$LOCAL_MODE" = true ]; then
    warn "Docker installed but daemon not running. Image builds will use CodeBuild fallback."
  fi
elif [ "$LOCAL_MODE" = true ]; then
  warn "Docker not installed. Image builds will use CodeBuild fallback."
fi

# Node.js (only for --local)
if command -v node &>/dev/null; then
  NODE_VERSION=$(node --version)
  NODE_MAJOR=$(echo "$NODE_VERSION" | sed 's/v//' | cut -d. -f1)
  if [ "$NODE_MAJOR" -ge 22 ]; then
    pass "Node.js: $NODE_VERSION"
  elif [ "$LOCAL_MODE" = true ]; then
    fail "Node.js $NODE_VERSION is too old (need >= 22). Update: https://nodejs.org/"
  fi
elif [ "$LOCAL_MODE" = true ]; then
  fail "Node.js not installed (required for --local). Install: https://nodejs.org/"
fi

section "CLI Tools — Post-Deploy Validation"

# kubectl (needed to verify pods, check logs, port-forward)
if command -v kubectl &>/dev/null; then
  KUBECTL_VERSION=$(kubectl version --client -o json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['clientVersion']['gitVersion'])" 2>/dev/null || kubectl version --client 2>&1 | head -1)
  pass "kubectl: $KUBECTL_VERSION"
else
  warn "kubectl not installed — needed to verify deployment (pod status, logs, health checks)"
  warn "  Install: https://kubernetes.io/docs/tasks/tools/"
fi

# GitHub CLI (optional, for agent testing)
if command -v gh &>/dev/null; then
  pass "GitHub CLI: $(gh --version | head -1)"
else
  warn "GitHub CLI not installed — optional, used for agent testing and issue management"
fi

# curl (for health endpoint checks)
if command -v curl &>/dev/null; then
  pass "curl: available"
else
  warn "curl not installed — needed for health endpoint verification"
fi

# =============================================================================
# 2. AWS Configuration
# =============================================================================
section "AWS Configuration"

# Credentials
if aws sts get-caller-identity &>/dev/null; then
  ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
  CALLER_ARN=$(aws sts get-caller-identity --query Arn --output text)
  pass "AWS credentials valid: $CALLER_ARN"
  pass "Account ID: $ACCOUNT_ID"
else
  fail "AWS credentials not configured or expired. Run: aws configure"
  # Can't check anything else without credentials
  echo ""
  echo -e "Results: ${GREEN}$PASS passed${NC}, ${YELLOW}$WARN warnings${NC}, ${RED}$FAIL failed${NC}"
  exit 1
fi

# Config-vs-identity match — only checked when config/deployment.yml is
# present and pinned to a specific account_id. If the file is absent, the
# helper falls back to the caller's identity, so by definition they match.
_CONFIG_FILE_PATH="$(cd "$SCRIPT_DIR/../.." && pwd)/config/deployment.yml"
if [ -f "$_CONFIG_FILE_PATH" ] && [ -n "$ADP_ACCOUNT_ID" ]; then
  if [ "$ADP_ACCOUNT_ID" = "$ACCOUNT_ID" ]; then
    pass "config/deployment.yml account_id matches caller identity"
  else
    fail "config/deployment.yml says account_id=$ADP_ACCOUNT_ID but caller is $ACCOUNT_ID. Either fix config or switch AWS_PROFILE."
  fi
fi

# Region
AWS_REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || echo "")}"
if [ -n "$AWS_REGION" ]; then
  pass "AWS Region: $AWS_REGION"
else
  fail "AWS Region not set. Run: export AWS_REGION=us-east-1"
fi

# =============================================================================
# 3. AWS Permissions
# =============================================================================
section "AWS Permissions"

# S3 (for Terraform state)
if aws s3 ls &>/dev/null; then
  pass "S3: ListBuckets OK"
else
  fail "S3: cannot list buckets (need s3:ListAllMyBuckets)"
fi

# DynamoDB (for Terraform locks)
if aws dynamodb list-tables --max-items 1 --region "${AWS_REGION:-us-east-1}" &>/dev/null; then
  pass "DynamoDB: ListTables OK"
else
  fail "DynamoDB: cannot list tables"
fi

# EKS
if aws eks list-clusters --region "${AWS_REGION:-us-east-1}" &>/dev/null; then
  pass "EKS: ListClusters OK"
else
  fail "EKS: cannot list clusters (need eks:ListClusters)"
fi

# ECR
if aws ecr describe-repositories --region "${AWS_REGION:-us-east-1}" --max-items 1 &>/dev/null; then
  pass "ECR: DescribeRepositories OK"
else
  fail "ECR: cannot describe repositories"
fi

# IAM (for CodeBuild role creation)
if aws iam get-user &>/dev/null || aws iam list-roles --max-items 1 &>/dev/null; then
  pass "IAM: read access OK"
else
  warn "IAM: limited access (may need iam:CreateRole for CodeBuild)"
fi

# CodeBuild
# NOTE: `codebuild list-projects` does not accept --max-results; passing it
# makes the CLI exit non-zero on a usage error, which masquerades as a missing
# permission. Use the bare call so a failure reflects a real perm/region issue.
if aws codebuild list-projects --region "${AWS_REGION:-us-east-1}" &>/dev/null; then
  pass "CodeBuild: ListProjects OK"
else
  warn "CodeBuild: cannot list projects (needed for default AWS deploy)"
fi

# Bedrock (for gateway proxy)
# NOTE: `bedrock list-foundation-models` does not accept --max-results either;
# bound the output with --query instead so a failure reflects a real issue.
if aws bedrock list-foundation-models --region "${AWS_REGION:-us-east-1}" --query 'modelSummaries[0].modelId' &>/dev/null; then
  pass "Bedrock: ListFoundationModels OK"
else
  warn "Bedrock: cannot list models (gateway needs bedrock:InvokeModel at runtime)"
fi

# Bedrock marketplace agreements — fresh accounts have none, and without them
# every Claude invoke fails with AccessDeniedException (the agent-worker then
# misreports the failure as "no changes needed"). deploy-all.sh and
# platform-infra-apply.yml accept them via enable-bedrock-models.sh.
for model in anthropic.claude-opus-4-6-v1 anthropic.claude-sonnet-4-6; do
  AGREEMENT=$(aws bedrock get-foundation-model-availability --model-id "$model" \
    --region "${AWS_REGION:-us-east-1}" \
    --query 'agreementAvailability.status' --output text 2>/dev/null || echo "UNKNOWN")
  if [ "$AGREEMENT" = "AVAILABLE" ]; then
    pass "Bedrock agreement: $model active"
  else
    warn "Bedrock agreement: $model is $AGREEMENT — deploy will accept it via enable-bedrock-models.sh"
  fi
done

# Secrets Manager
if aws secretsmanager list-secrets --region "${AWS_REGION:-us-east-1}" --max-results 1 &>/dev/null; then
  pass "Secrets Manager: ListSecrets OK"
else
  warn "Secrets Manager: limited access (needed for agent-factory GitHub App credentials)"
fi

# CloudFormation/Cognito/RDS/ElastiCache (spot checks)
if aws cognito-idp list-user-pools --max-results 1 --region "${AWS_REGION:-us-east-1}" &>/dev/null; then
  pass "Cognito: ListUserPools OK"
else
  warn "Cognito: limited access (gateway needs cognito-idp:* for auth)"
fi

# Lambda concurrency quota (Issue #2910)
# Gateway lambdas reserve 97 total (50+20+10+10+5+2). AWS requires the account's
# UNRESERVED pool to stay >= 100 after all reservations, so
# UnreservedConcurrentExecutions must be >= 197 at apply time.
#
# Read UnreservedConcurrentExecutions (the pool actually available for new
# reservations), NOT ConcurrentExecutions (the total account quota). On SCP-locked
# or shared accounts another reserved lambda can consume most of the total quota
# and pin the unreserved pool at the 100 floor while the total quota still reads
# 1000 — the total-quota reading would report a false PASS there (Issue #2910
# escalation, account 979157915401 with WSConcurrencyCurtailer reserving ~890).
LAMBDA_UNRESERVED=$(aws lambda get-account-settings --region "${AWS_REGION:-us-east-1}" --query 'AccountLimit.UnreservedConcurrentExecutions' --output text 2>/dev/null || echo "")
if [ -n "$LAMBDA_UNRESERVED" ] && [ "$LAMBDA_UNRESERVED" != "None" ]; then
  # Gateway lambdas reserve 97 total; unreserved pool must stay >= 100 afterward.
  REQUIRED_UNRESERVED=197  # 97 (reserved) + 100 (AWS minimum unreserved)
  if [ "$LAMBDA_UNRESERVED" -ge "$REQUIRED_UNRESERVED" ]; then
    pass "Lambda unreserved concurrency: $LAMBDA_UNRESERVED (>= $REQUIRED_UNRESERVED needed for gateway reserved concurrency)"
  else
    warn "Lambda unreserved concurrency: $LAMBDA_UNRESERVED (< $REQUIRED_UNRESERVED). Gateway reserved concurrency will fail (PutFunctionConcurrency below the 100-unreserved minimum)."
    warn "  Either request a Service Quotas increase for L-B99A9384 (if the total quota is low), free reserved concurrency held by other functions, or set enable_lambda_reserved_concurrency=false in gateway.tfvars."
  fi
else
  warn "Lambda: could not read account concurrency settings"
fi

# =============================================================================
# 4. Existing Infrastructure (if any)
# =============================================================================
section "Existing Infrastructure"

STATE_BUCKET="adp-terraform-state-${ACCOUNT_ID}"
if aws s3api head-bucket --bucket "$STATE_BUCKET" 2>/dev/null; then
  pass "Terraform state bucket exists: $STATE_BUCKET"
else
  warn "Terraform state bucket not found: $STATE_BUCKET (will be created by bootstrap)"
fi

LOCK_TABLE="adp-terraform-locks"
if aws dynamodb describe-table --table-name "$LOCK_TABLE" --region "${AWS_REGION:-us-east-1}" &>/dev/null; then
  pass "Terraform lock table exists: $LOCK_TABLE"
else
  warn "Terraform lock table not found: $LOCK_TABLE (will be created by bootstrap)"
fi

EKS_CLUSTER="adp-${ENVIRONMENT:-dev}-eks-cluster"
if aws eks describe-cluster --name "$EKS_CLUSTER" --region "${AWS_REGION:-us-east-1}" &>/dev/null; then
  EKS_STATUS=$(aws eks describe-cluster --name "$EKS_CLUSTER" --region "${AWS_REGION:-us-east-1}" --query 'cluster.status' --output text)
  pass "EKS cluster exists: $EKS_CLUSTER ($EKS_STATUS)"
else
  warn "EKS cluster not found: $EKS_CLUSTER (will be created by platform deploy)"
fi

# Check ECR repo
if aws ecr describe-repositories --repository-names adp-gateway --region "${AWS_REGION:-us-east-1}" &>/dev/null; then
  pass "ECR repository exists: adp-gateway"
else
  warn "ECR repository not found: adp-gateway (will be created by platform deploy)"
fi

# =============================================================================
# 5. Environment config
# =============================================================================
section "Environment Configuration"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

if [ -f "$ROOT_DIR/environments/dev/backend.tfvars" ]; then
  if grep -q "ACCOUNT_ID" "$ROOT_DIR/environments/dev/backend.tfvars"; then
    warn "environments/dev/backend.tfvars still has ACCOUNT_ID placeholder (bootstrap will fix this)"
  else
    pass "environments/dev/backend.tfvars configured"
  fi
else
  warn "environments/dev/backend.tfvars not found"
fi

if [ -f "$ROOT_DIR/environments/dev/platform.tfvars" ]; then
  pass "environments/dev/platform.tfvars exists"
else
  warn "environments/dev/platform.tfvars not found"
fi

if [ -f "$ROOT_DIR/environments/dev/modules/gateway.tfvars" ]; then
  pass "environments/dev/modules/gateway.tfvars exists"
else
  warn "environments/dev/modules/gateway.tfvars not found"
fi

# =============================================================================
# Summary
# =============================================================================
echo ""
echo "==========================================="
echo -e "Results: ${GREEN}$PASS passed${NC}, ${YELLOW}$WARN warnings${NC}, ${RED}$FAIL failed${NC}"
echo "==========================================="

if [ "$FAIL" -gt 0 ]; then
  echo ""
  echo -e "${RED}Fix the failures above before deploying.${NC}"
  exit 1
elif [ "$WARN" -gt 0 ]; then
  echo ""
  echo -e "${YELLOW}Warnings are non-blocking but may limit post-deploy validation.${NC}"
  if [ "$LOCAL_MODE" = false ]; then
    echo "Default (AWS) deploy requires only: aws + zip"
    echo "Post-deploy validation also needs: kubectl, curl"
  else
    echo "Local deploy requires: aws, terraform, docker, node >= 22, kubectl"
  fi
  exit 0
else
  echo ""
  echo -e "${GREEN}All checks passed. Ready to deploy.${NC}"
  exit 0
fi
