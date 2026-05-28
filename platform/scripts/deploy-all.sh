#!/bin/bash
set -euo pipefail

# =============================================================================
# ADP — Deploy Everything
# =============================================================================
# Deploys the entire platform. Requires: AWS CLI, Terraform, Node.js, kubectl.
# Docker builds use CodeBuild (4 Terraform-managed projects in
# platform/infra/modules/codebuild/). Everything else runs directly.
#
# Usage:
#   ./platform/scripts/deploy-all.sh                        # Deploy all modules
#   ./platform/scripts/deploy-all.sh --gateway-only         # Platform + gateway only
#   ./platform/scripts/deploy-all.sh --agent-context-only   # Platform + agent-context only
#   ./platform/scripts/deploy-all.sh --destroy              # Tear down everything
#   ./platform/scripts/deploy-all.sh --local                # Run everything locally (needs Terraform, Docker, Node, kubectl)
#   ./platform/scripts/deploy-all.sh --ci                   # CI mode: validate outputs exist without re-applying
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Load deployment config — populates ADP_ACCOUNT_ID, ADP_REGION,
# ADP_ENVIRONMENT, ADP_GITHUB_ORG, etc. Falls back to runtime defaults
# (aws sts get-caller-identity, AWS_REGION env, etc.) when no config
# file is present, so existing self-managed deploys keep working.
# shellcheck source=load-deploy-config.sh
source "${SCRIPT_DIR}/load-deploy-config.sh"

AWS_REGION="$ADP_REGION"
ENVIRONMENT="$ADP_ENVIRONMENT"
GATEWAY_ONLY=false
AGENT_FACTORY_ONLY=false
AGENT_CONTEXT_ONLY=false
SKIP_AGENT_CONTEXT=false
AGENT_CONTEXT_ENABLED="${AGENT_CONTEXT_ENABLED:-false}"
DESTROY=false
SKIP_FRONTEND=false
LOCAL_MODE=false
CI_MODE=false

for arg in "$@"; do
  case $arg in
    --gateway-only) GATEWAY_ONLY=true ;;
    --agent-factory-only) AGENT_FACTORY_ONLY=true ;;
    --agent-context-only) AGENT_CONTEXT_ONLY=true ;;
    --skip-agent-context) SKIP_AGENT_CONTEXT=true ;;
    --destroy) DESTROY=true ;;
    --skip-frontend) SKIP_FRONTEND=true ;;
    --local) LOCAL_MODE=true ;;
    --ci) CI_MODE=true ;;
    --help)
      echo "Usage: $0 [--gateway-only] [--agent-factory-only] [--agent-context-only] [--skip-agent-context] [--skip-frontend] [--local] [--destroy]"
      echo ""
      echo "  (default)              Deploy all modules (platform + gateway + agent-factory; agent-context if AGENT_CONTEXT_ENABLED=true)"
      echo "  --gateway-only         Deploy platform + gateway only (skip agent-factory, agent-context)"
      echo "  --agent-factory-only   Deploy platform + agent-factory only (skip gateway, agent-context)"
      echo "  --agent-context-only   Deploy platform + agent-context only (skip gateway, agent-factory)"
      echo "  --skip-agent-context   Skip agent-context even if AGENT_CONTEXT_ENABLED=true"
      echo "  --skip-frontend        Skip frontend build and deploy"
      echo "  --local                Use local Docker daemon for image builds (instead of CodeBuild)"
      echo "  --ci                   CI mode: validate outputs exist without re-applying (use after GH Actions deploys)"
      echo "  --destroy              Tear down all infrastructure"
      exit 0
      ;;
  esac
done

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
step() { echo -e "\n${BLUE}━━━ $1 ━━━${NC}\n"; }
ok()   { echo -e "${GREEN}✓ $1${NC}"; }
warn() { echo -e "${YELLOW}⚠ $1${NC}"; }
fail() { echo -e "${RED}✗ $1${NC}"; exit 1; }

# =============================================================================
# Preflight
# =============================================================================
step "Preflight checks"

echo "Running preflight validation..."
LOCAL_FLAG=""
[ "$LOCAL_MODE" = true ] && LOCAL_FLAG="--local"
if [ -f "$SCRIPT_DIR/preflight-check.sh" ]; then
  bash "$SCRIPT_DIR/preflight-check.sh" $LOCAL_FLAG || fail "Preflight checks failed. Fix the issues above and retry."
else
  warn "preflight-check.sh not found, skipping validation"
fi

command -v aws >/dev/null 2>&1 || fail "aws CLI not found"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) || fail "AWS CLI not configured"

# When config/deployment.yml pins a specific account_id, fail-fast if the
# operator's creds resolve elsewhere — mirrors preflight's safety check.
if [ -f "$ROOT_DIR/config/deployment.yml" ] && [ -n "$ADP_ACCOUNT_ID" ] && [ "$ADP_ACCOUNT_ID" != "$ACCOUNT_ID" ]; then
  fail "config/deployment.yml says account_id=$ADP_ACCOUNT_ID but caller resolves to $ACCOUNT_ID. Either fix config/deployment.yml or switch AWS_PROFILE."
fi

ok "AWS Account: $ACCOUNT_ID | Region: $AWS_REGION | Env: $ENVIRONMENT"

# ---------------------------------------------------------------------------
# Detect operator's public IP and lock EKS public API to /32 (portable)
# ---------------------------------------------------------------------------
# Anyone cloning this repo can run the script without editing tfvars. The
# detected IP is exported as TF_VAR_eks_public_access_cidrs so Terraform picks
# it up. If the caller already set the env var, respect it.
if [ -z "${TF_VAR_eks_public_access_cidrs:-}" ]; then
  MY_IP=""
  for url in https://checkip.amazonaws.com https://api.ipify.org https://ifconfig.me; do
    MY_IP=$(curl -fsS --max-time 5 "$url" 2>/dev/null | tr -d '[:space:]' || true)
    [ -n "$MY_IP" ] && break
  done
  if [ -z "$MY_IP" ]; then
    fail "Could not detect your public IP. Set TF_VAR_eks_public_access_cidrs='[\"<ip>/32\"]' manually."
  fi
  export TF_VAR_eks_public_access_cidrs="[\"${MY_IP}/32\"]"
  ok "EKS public API will allow: ${MY_IP}/32 (your current public IP)"
else
  ok "EKS public API CIDRs: ${TF_VAR_eks_public_access_cidrs} (from env)"
fi

REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
STATE_BUCKET="adp-terraform-state-${ACCOUNT_ID}"
LOCK_TABLE="adp-terraform-locks"
EKS_CLUSTER="adp-${ENVIRONMENT}-eks-cluster"
CB_ROLE_NAME="adp-${ENVIRONMENT}-codebuild-role"

# =============================================================================
# Helper: ensure CodeBuild IAM role exists
# =============================================================================
# The role and the 4 docker-build projects are Terraform-managed in
# platform/infra/modules/codebuild/. This helper only validates the role
# exists (it should after platform infra apply). If missing (bootstrap
# chicken-and-egg), it creates it imperatively as a fallback.
ensure_codebuild_role() {
  if aws iam get-role --role-name "$CB_ROLE_NAME" 2>/dev/null > /dev/null; then return; fi
  echo "Creating CodeBuild service role (bootstrap fallback)..."
  aws iam create-role --role-name "$CB_ROLE_NAME" \
    --assume-role-policy-document '{
      "Version":"2012-10-17",
      "Statement":[{"Effect":"Allow","Principal":{"Service":"codebuild.amazonaws.com"},"Action":"sts:AssumeRole"}]
    }' > /dev/null
  aws iam attach-role-policy --role-name "$CB_ROLE_NAME" \
    --policy-arn "arn:aws:iam::aws:policy/AdministratorAccess"
  echo "Waiting for IAM propagation..."
  sleep 15
  ok "CodeBuild role created with AdministratorAccess"
}

# =============================================================================
# Helper: package repo source and upload to S3
# =============================================================================
upload_source() {
  local ZIP_PATH="/tmp/adp-deploy-source.zip"
  echo "Packaging repository source..."
  # Use shared zip script to keep excludes in sync with gateway-deploy.yml
  bash "$SCRIPT_DIR/zip-source.sh" "$ROOT_DIR" "$ZIP_PATH"
  aws s3 cp "$ZIP_PATH" "s3://${STATE_BUCKET}/codebuild/adp-source.zip" --region "$AWS_REGION" > /dev/null
  rm -f "$ZIP_PATH"
  ok "Source uploaded to s3://${STATE_BUCKET}/codebuild/adp-source.zip"
}

# =============================================================================
# Helper: run a CodeBuild job (project must already exist via Terraform)
# =============================================================================
# The 4 docker-build projects are Terraform-managed in
# platform/infra/modules/codebuild/. This function only starts builds
# against existing projects — it no longer creates or updates them.
run_codebuild() {
  local PROJECT_NAME="$1"
  local BUILDSPEC_FILE="$2"  # unused — buildspec is baked into the project

  # Verify the project exists
  local PROJECT_EXISTS
  PROJECT_EXISTS=$(aws codebuild batch-get-projects --names "$PROJECT_NAME" --region "$AWS_REGION" \
    --query 'projects | length(@)' --output text 2>/dev/null || echo "0")
  if [ "$PROJECT_EXISTS" -eq 0 ]; then
    fail "CodeBuild project '$PROJECT_NAME' not found. Run 'terraform apply' in platform/infra/ first."
  fi

  # Start build
  local BUILD_ID
  BUILD_ID=$(aws codebuild start-build --region "$AWS_REGION" --project-name "$PROJECT_NAME" \
    --environment-variables-override \
      "name=AWS_REGION,value=$AWS_REGION" \
      "name=ENVIRONMENT,value=$ENVIRONMENT" \
      "name=ACCOUNT_ID,value=$ACCOUNT_ID" \
      "name=REGISTRY,value=$REGISTRY" \
      "name=STATE_BUCKET,value=$STATE_BUCKET" \
      "name=EKS_CLUSTER,value=$EKS_CLUSTER" \
    --query 'build.id' --output text)
  echo "  Build: $BUILD_ID"

  # Stream-wait for completion
  local PHASE=""
  while true; do
    local RESULT
    RESULT=$(aws codebuild batch-get-builds --ids "$BUILD_ID" --region "$AWS_REGION" \
      --query 'builds[0].{s:buildStatus,p:currentPhase}' --output json 2>/dev/null)
    local STATUS=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['s'])" 2>/dev/null || echo "IN_PROGRESS")
    local CUR_PHASE=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['p'])" 2>/dev/null || echo "")
    [ "$CUR_PHASE" != "$PHASE" ] && [ -n "$CUR_PHASE" ] && echo "  Phase: $CUR_PHASE" && PHASE="$CUR_PHASE"
    case "$STATUS" in
      SUCCEEDED) ok "Build succeeded: $PROJECT_NAME"; return 0 ;;
      FAILED|FAULT|STOPPED|TIMED_OUT)
        echo "  Fetching logs..."
        aws codebuild batch-get-builds --ids "$BUILD_ID" --region "$AWS_REGION" \
          --query 'builds[0].logs.deepLink' --output text 2>/dev/null || true
        fail "Build failed ($STATUS): $PROJECT_NAME" ;;
      *) sleep 15 ;;
    esac
  done
}

# =============================================================================
# DESTROY — Tear down all infrastructure in reverse deploy order
# =============================================================================
# Uses the same shared scripts as the per-module destroy workflows:
#   - delete-ingress-and-wait.sh  (ALB cleanup before gateway destroy)
#   - empty-s3-buckets.sh         (non-empty buckets block terraform destroy)
#   - force-delete-secrets.sh     (avoid 7-day collision on re-deploy)
#
# Order: agent-context → agent-factory → gateway → platform
# State backend (S3 + DynamoDB) is NOT destroyed — use bootstrap-destroy.sh.
# GitHub App secrets (adp/gh-app-*) are NOT touched — survive by design.
# =============================================================================
if [ "$DESTROY" = true ]; then
  step "Destroying all infrastructure"
  echo "This will destroy ALL ADP infrastructure in $ENVIRONMENT."
  echo ""
  echo "Destroy order: agent-context → agent-factory → gateway → platform"
  echo "State backend and GitHub App secrets will NOT be deleted."
  echo ""
  echo "Type 'yes' to proceed:"
  read -r confirm
  [ "$confirm" = "yes" ] || { echo "Aborted."; exit 0; }

  # Configure kubectl for K8s cleanup steps
  if command -v kubectl >/dev/null 2>&1; then
    aws eks update-kubeconfig --name "$EKS_CLUSTER" --region "$AWS_REGION" 2>/dev/null || true
  fi

  # -------------------------------------------------------------------------
  # 1. Agent Context
  # -------------------------------------------------------------------------
  step "Destroy 1/4: Agent Context"
  if [ -d "$ROOT_DIR/modules/agent-context/terraform" ]; then
    kubectl delete namespace agent-context --wait=true --timeout=120s 2>/dev/null || true
    cd "$ROOT_DIR/modules/agent-context/terraform"
    terraform init -backend-config="../../../environments/$ENVIRONMENT/modules/agent-context-backend.tfvars" -input=false 2>/dev/null || true
    terraform destroy -var-file="../../../environments/$ENVIRONMENT/modules/agent-context.tfvars" -auto-approve || true
    ok "Agent Context destroyed"
  else
    ok "Agent Context: not present, skipping"
  fi

  # -------------------------------------------------------------------------
  # 2. Agent Factory
  # -------------------------------------------------------------------------
  step "Destroy 2/4: Agent Factory"
  if [ -f "$ROOT_DIR/modules/agent-factory/infra/terraform.tfvars" ]; then
    # Clean up K8s resources
    kubectl delete scaledjobs --all -n adp-gateway-agents 2>/dev/null || true
    kubectl delete namespace adp-gateway-agents --wait=true --timeout=120s 2>/dev/null || true
    kubectl delete namespace arc-runners --wait=true --timeout=120s 2>/dev/null || true

    # Empty S3 buckets (beads state, chat artifacts)
    FACTORY_BUCKETS=""
    for pattern in "adp-${ENVIRONMENT}-agent-beads-state-" "adp-${ENVIRONMENT}-chat-artifacts-"; do
      FOUND=$(aws s3api list-buckets --query "Buckets[?starts_with(Name,'${pattern}')].Name" --output text 2>/dev/null || echo "")
      [ -n "$FOUND" ] && [ "$FOUND" != "None" ] && FACTORY_BUCKETS="$FACTORY_BUCKETS $FOUND"
    done
    [ -n "$FACTORY_BUCKETS" ] && bash "$SCRIPT_DIR/empty-s3-buckets.sh" $FACTORY_BUCKETS

    cd "$ROOT_DIR/modules/agent-factory/infra"
    terraform init -backend-config="../../../environments/$ENVIRONMENT/modules/agent-factory-backend.tfvars" -input=false 2>/dev/null || true
    terraform destroy -var-file=terraform.tfvars -auto-approve || true
    ok "Agent Factory destroyed"
  else
    ok "Agent Factory: not configured, skipping"
  fi

  # -------------------------------------------------------------------------
  # 3. Gateway (most complex — ALB, S3, Secrets, CloudFront cleanup first)
  # -------------------------------------------------------------------------
  step "Destroy 3/4: Gateway"

  # 3a. Delete Ingress and wait for ALB to be removed by the controller
  echo "Cleaning up Ingress resources and ALBs..."
  bash "$SCRIPT_DIR/delete-ingress-and-wait.sh" || true

  # 3b. Delete remaining K8s gateway resources
  kubectl delete namespace adp-gateway --wait=true --timeout=120s 2>/dev/null || true

  # 3c. Empty S3 buckets (frontend, etc.)
  GW_BUCKETS=""
  FRONTEND_BUCKET=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/frontend-bucket" \
    --query "Parameter.Value" --output text --region "$AWS_REGION" 2>/dev/null || echo "")
  [ -n "$FRONTEND_BUCKET" ] && [ "$FRONTEND_BUCKET" != "None" ] && GW_BUCKETS="$FRONTEND_BUCKET"
  for pattern in "bedrockgw-${ENVIRONMENT}-frontend-"; do
    FOUND=$(aws s3api list-buckets --query "Buckets[?starts_with(Name,'${pattern}')].Name" --output text 2>/dev/null || echo "")
    [ -n "$FOUND" ] && [ "$FOUND" != "None" ] && GW_BUCKETS="$GW_BUCKETS $FOUND"
  done
  [ -n "$GW_BUCKETS" ] && bash "$SCRIPT_DIR/empty-s3-buckets.sh" $GW_BUCKETS

  # 3d. Force-delete Secrets Manager secrets (avoid 7-day collision)
  bash "$SCRIPT_DIR/force-delete-secrets.sh" \
    "bedrockgw-${ENVIRONMENT}-" \
    "adp/${ENVIRONMENT}/gateway/test-" || true

  # 3e. Disable CloudFront distribution (two-phase delete)
  DIST_ID=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/cloudfront-id" \
    --query "Parameter.Value" --output text --region "$AWS_REGION" 2>/dev/null || echo "")
  if [ -n "$DIST_ID" ] && [ "$DIST_ID" != "None" ]; then
    echo "Disabling CloudFront distribution $DIST_ID..."
    DIST_CONFIG=$(aws cloudfront get-distribution-config --id "$DIST_ID" 2>/dev/null || echo "")
    if [ -n "$DIST_CONFIG" ]; then
      ENABLED=$(echo "$DIST_CONFIG" | python3 -c "import sys,json; d=json.load(sys.stdin); print(str(d['DistributionConfig']['Enabled']).lower())" 2>/dev/null || echo "false")
      if [ "$ENABLED" = "true" ]; then
        ETAG=$(echo "$DIST_CONFIG" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['ETag'])")
        echo "$DIST_CONFIG" | python3 -c "
import sys, json
d = json.load(sys.stdin)
config = d['DistributionConfig']
config['Enabled'] = False
print(json.dumps(config))
" > /tmp/cf-disable-config.json
        aws cloudfront update-distribution --id "$DIST_ID" --if-match "$ETAG" \
          --distribution-config "file:///tmp/cf-disable-config.json" > /dev/null 2>&1 || true
        echo "Waiting for CloudFront to deploy disabled state (up to 15 min)..."
        aws cloudfront wait distribution-deployed --id "$DIST_ID" 2>/dev/null || {
          warn "CloudFront wait timed out. terraform destroy may retry."
        }
        rm -f /tmp/cf-disable-config.json
        ok "CloudFront $DIST_ID disabled"
      else
        ok "CloudFront $DIST_ID already disabled"
      fi
    fi
  fi

  # 3f. Terraform destroy
  cd "$ROOT_DIR/modules/gateway/infra"
  terraform init -backend-config="../../../environments/$ENVIRONMENT/modules/gateway-backend.tfvars" -input=false 2>/dev/null || true
  terraform destroy -var-file="../../../environments/$ENVIRONMENT/modules/gateway.tfvars" -auto-approve || true

  # 3g. Clean up SSM parameters
  for param in "/adp/$ENVIRONMENT/gateway/frontend-bucket" \
               "/adp/$ENVIRONMENT/gateway/cloudfront-id" \
               "/adp/$ENVIRONMENT/gateway/cloudfront-domain" \
               "/adp/$ENVIRONMENT/gateway/internal-alb-arn" \
               "/adp/$ENVIRONMENT/gateway/internal-alb-dns" \
               "/adp/$ENVIRONMENT/gateway/internal-alb-security-group-ids"; do
    aws ssm delete-parameter --name "$param" --region "$AWS_REGION" 2>/dev/null || true
  done
  ok "Gateway destroyed"

  # -------------------------------------------------------------------------
  # 4. Platform (last — EKS, VPC, ECR, IAM)
  # -------------------------------------------------------------------------
  step "Destroy 4/4: Platform"

  # Clean up K8s system namespaces before cluster destroy
  kubectl delete namespace arc-systems --wait=true --timeout=120s 2>/dev/null || true
  kubectl delete namespace keda --wait=true --timeout=120s 2>/dev/null || true

  # Clean up orphaned ENIs
  VPC_ID=$(aws eks describe-cluster --name "$EKS_CLUSTER" --region "$AWS_REGION" \
    --query 'cluster.resourcesVpcConfig.vpcId' --output text 2>/dev/null || echo "")
  if [ -n "$VPC_ID" ] && [ "$VPC_ID" != "None" ]; then
    ORPHAN_ENIS=$(aws ec2 describe-network-interfaces --region "$AWS_REGION" \
      --filters "Name=vpc-id,Values=$VPC_ID" "Name=status,Values=available" \
      --query 'NetworkInterfaces[].NetworkInterfaceId' --output text 2>/dev/null || echo "")
    for ENI in $ORPHAN_ENIS; do
      echo "  Deleting orphaned ENI: $ENI"
      aws ec2 delete-network-interface --network-interface-id "$ENI" --region "$AWS_REGION" 2>/dev/null || true
    done
  fi

  cd "$ROOT_DIR/platform/infra"
  terraform init -backend-config="../../environments/$ENVIRONMENT/backend.tfvars" -input=false 2>/dev/null || true
  terraform destroy -var-file="../../environments/$ENVIRONMENT/platform.tfvars" -auto-approve || true

  # Clean up any leftover retired CodeBuild projects
  for p in "adp-${ENVIRONMENT}-frontend-build" "adp-${ENVIRONMENT}-platform-infra" "adp-${ENVIRONMENT}-gateway-deploy" "adp-${ENVIRONMENT}-gateway-infra" "adp-${ENVIRONMENT}-gateway-alb-wire" "adp-${ENVIRONMENT}-agent-factory-infra" "adp-${ENVIRONMENT}-agent-context-infra" "adp-${ENVIRONMENT}-agent-context-deploy" "adp-${ENVIRONMENT}-destroy"; do
    aws codebuild delete-project --name "$p" --region "$AWS_REGION" 2>/dev/null || true
  done
  ok "Platform destroyed"

  # -------------------------------------------------------------------------
  # Summary
  # -------------------------------------------------------------------------
  step "Destroy complete"
  echo "All module infrastructure has been destroyed."
  echo ""
  echo "Surviving resources (by design):"
  echo "  - Terraform state backend: S3 $STATE_BUCKET + DynamoDB $LOCK_TABLE"
  echo "  - GitHub App secrets: adp/gh-app-* in Secrets Manager"
  echo ""
  echo "To destroy the state backend (only after verifying all modules are gone):"
  echo "  $SCRIPT_DIR/bootstrap-destroy.sh"
  exit 0
fi

# =============================================================================
# CI mode: validate module outputs exist without re-applying
# =============================================================================
if [ "$CI_MODE" = true ]; then
  step "CI Validation Mode"
  echo "Checking that Terraform outputs exist for each module."
  echo "If any are missing, run the matching GitHub Actions workflow."
  echo ""
  CI_FAILURES=0
  GH_REPO_URL="https://github.com/$(git remote get-url origin 2>/dev/null | sed 's|.*github.com[:/]||;s|\.git$||' || echo 'UNKNOWN')"

  # Platform
  ci_check_module() {
    local MODULE_NAME="$1"
    local STATE_KEY="$2"
    local WORKFLOW_FILE="$3"
    echo -n "  $MODULE_NAME: "
    if aws s3 cp "s3://${STATE_BUCKET}/${STATE_KEY}" /tmp/ci-check-state.json --region "$AWS_REGION" >/dev/null 2>&1; then
      OUTPUTS=$(python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('outputs',{})))" < /tmp/ci-check-state.json 2>/dev/null || echo "0")
      rm -f /tmp/ci-check-state.json
      if [ "$OUTPUTS" -gt 0 ]; then
        ok "$OUTPUTS output(s) found"
      else
        warn "State exists but 0 outputs. Run: $GH_REPO_URL/actions/workflows/$WORKFLOW_FILE"
        CI_FAILURES=$((CI_FAILURES + 1))
      fi
    else
      warn "No state found. Run: $GH_REPO_URL/actions/workflows/$WORKFLOW_FILE"
      CI_FAILURES=$((CI_FAILURES + 1))
    fi
  }

  ci_check_module "platform"      "${ENVIRONMENT}/platform/terraform.tfstate"             "platform-infra-apply.yml"
  if [ "$AGENT_FACTORY_ONLY" = false ] && [ "$AGENT_CONTEXT_ONLY" = false ]; then
    ci_check_module "gateway"       "${ENVIRONMENT}/modules/gateway/terraform.tfstate"      "gateway-infra-apply.yml"
  fi
  if [ "$GATEWAY_ONLY" = false ] && [ "$AGENT_CONTEXT_ONLY" = false ]; then
    ci_check_module "agent-factory" "${ENVIRONMENT}/modules/agent-factory/terraform.tfstate" "agent-factory-infra-apply.yml"
  fi
  if [ "$AGENT_CONTEXT_ENABLED" = true ] || [ "$AGENT_CONTEXT_ONLY" = true ]; then
    ci_check_module "agent-context" "${ENVIRONMENT}/modules/agent-context/terraform.tfstate" "agent-context-infra-apply.yml"
  fi

  # Also check EKS cluster directly
  echo ""
  echo -n "  EKS cluster: "
  EKS_STATUS=$(aws eks describe-cluster --name "$EKS_CLUSTER" --query 'cluster.status' --output text --region "$AWS_REGION" 2>/dev/null || echo "NOT_FOUND")
  if [ "$EKS_STATUS" = "ACTIVE" ]; then
    ok "ACTIVE"
  else
    warn "$EKS_STATUS — Run: $GH_REPO_URL/actions/workflows/platform-infra-apply.yml"
    CI_FAILURES=$((CI_FAILURES + 1))
  fi

  echo ""
  if [ "$CI_FAILURES" -gt 0 ]; then
    fail "$CI_FAILURES module(s) missing outputs. Run the listed GitHub Actions workflows first."
  fi
  ok "All modules have outputs. Infrastructure is managed via GitHub Actions CI."
  exit 0
fi

# =============================================================================
# Step 1: Bootstrap (always local — chicken-and-egg)
# =============================================================================
step "Step 1/6: Bootstrap Terraform state backend"

if aws s3api head-bucket --bucket "$STATE_BUCKET" 2>/dev/null; then
  ok "State bucket exists: $STATE_BUCKET"
else
  echo "Creating S3 bucket and DynamoDB table..."
  aws s3api create-bucket --bucket "$STATE_BUCKET" --region "$AWS_REGION" \
    $([ "$AWS_REGION" != "us-east-1" ] && echo "--create-bucket-configuration LocationConstraint=$AWS_REGION") > /dev/null 2>&1
  aws s3api put-bucket-versioning --bucket "$STATE_BUCKET" --versioning-configuration Status=Enabled
  aws s3api put-bucket-encryption --bucket "$STATE_BUCKET" \
    --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
  aws s3api put-public-access-block --bucket "$STATE_BUCKET" \
    --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  ok "S3 bucket created: $STATE_BUCKET"
fi

if ! aws dynamodb describe-table --table-name "$LOCK_TABLE" --region "$AWS_REGION" > /dev/null 2>&1; then
  aws dynamodb create-table --table-name "$LOCK_TABLE" \
    --attribute-definitions AttributeName=LockID,AttributeType=S \
    --key-schema AttributeName=LockID,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST --region "$AWS_REGION" > /dev/null
  aws dynamodb wait table-exists --table-name "$LOCK_TABLE" --region "$AWS_REGION"
  ok "DynamoDB table created: $LOCK_TABLE"
else
  ok "DynamoDB table exists: $LOCK_TABLE"
fi

# Replace ACCOUNT_ID placeholders
find "$ROOT_DIR/environments/" -name "*.tfvars" 2>/dev/null | while read f; do
  if grep -q "ACCOUNT_ID" "$f" 2>/dev/null; then
    sed -i '' "s/ACCOUNT_ID/${ACCOUNT_ID}/g" "$f" 2>/dev/null || sed -i "s/ACCOUNT_ID/${ACCOUNT_ID}/g" "$f"
  fi
done
ok "Environment configs updated"

# =============================================================================
# Upload source for CodeBuild docker-build steps
# =============================================================================
# The 4 docker-build CodeBuild projects (gateway-build, chat-agent,
# agent-gateway, arc-runner) are Terraform-managed. Their buildspecs are
# checked into codebuild/bs-*.yml. All other work (terraform apply, npm build,
# kubectl apply) now runs directly — no CodeBuild needed.
# =============================================================================

# =============================================================================
# Step 2: Platform infra
# =============================================================================
step "Step 2/6: Deploy shared platform (VPC, EKS, ECR, IAM)"

# Platform infra runs directly (Terraform + kubectl) — no CodeBuild needed.
cd "$ROOT_DIR/platform/infra"
terraform init -backend-config="../../environments/$ENVIRONMENT/backend.tfvars" -input=false
terraform apply -var-file="../../environments/$ENVIRONMENT/platform.tfvars" -auto-approve
ok "Platform deployed"

# Configure kubectl (needed for k8s steps — local or CodeBuild deploy step)
if command -v kubectl >/dev/null 2>&1; then
  aws eks update-kubeconfig --name "$EKS_CLUSTER" --region "$AWS_REGION" 2>/dev/null || true
fi

# =============================================================================
# Step 3: Gateway infra
# =============================================================================
step "Step 3/6: Deploy gateway infrastructure"

if [ "$AGENT_FACTORY_ONLY" = true ] || [ "$AGENT_CONTEXT_ONLY" = true ]; then
  echo "Skipping gateway infra (--agent-factory-only or --agent-context-only)"
  ok "Skipped"
else
  # Gateway infra runs directly — no CodeBuild needed.
  cd "$ROOT_DIR/modules/gateway/infra"
  terraform init -backend-config="../../../environments/$ENVIRONMENT/modules/gateway-backend.tfvars" -input=false
  terraform apply -var-file="../../../environments/$ENVIRONMENT/modules/gateway.tfvars" -auto-approve
  ok "Gateway infrastructure deployed"
fi

# =============================================================================
# Step 4: Build + deploy gateway
# =============================================================================
step "Step 4/6: Build and deploy gateway"

if [ "$AGENT_FACTORY_ONLY" = true ] || [ "$AGENT_CONTEXT_ONLY" = true ]; then
  echo "Skipping gateway deploy (--agent-factory-only or --agent-context-only)"
  ok "Skipped"
else
  # --- Docker build: use CodeBuild (needs privileged mode) or local Docker ---
  if [ "$LOCAL_MODE" = true ] && docker info &>/dev/null 2>&1; then
    cd "$ROOT_DIR/modules/gateway"
    docker build -t adp-gateway .
    aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY"
    docker tag adp-gateway:latest "$REGISTRY/adp-gateway:latest"
    docker push "$REGISTRY/adp-gateway:latest"
  else
    # Docker build via CodeBuild (Terraform-managed project)
    upload_source
    run_codebuild "adp-${ENVIRONMENT}-gateway-build" "codebuild/bs-gateway-build.yml"
  fi

  # --- K8s deploy: runs directly (no CodeBuild needed) ---
  cd "$ROOT_DIR/modules/gateway/infra"
  DB_HOST=$(terraform output -raw rds_endpoint 2>/dev/null | sed 's/:5432//' || echo "localhost")
  DB_NAME=$(terraform output -raw rds_database_name 2>/dev/null || echo "bedrockgateway")
  DB_USER="bgadmin"
  REDIS_HOST=$(terraform output -raw redis_endpoint 2>/dev/null || echo "localhost")
  REDIS_PORT=$(terraform output -raw redis_port 2>/dev/null || echo "6379")
  COGNITO_USER_POOL_ID=$(terraform output -raw cognito_user_pool_id 2>/dev/null || echo "")
  COGNITO_CLIENT_ID=$(terraform output -raw cognito_user_pool_client_id 2>/dev/null || echo "")
  COGNITO_DOMAIN=$(terraform output -raw cognito_domain 2>/dev/null || echo "")
  CF_DOMAIN=$(terraform output -raw frontend_cloudfront_domain_name 2>/dev/null || echo "")
  AGENT_REGISTRY_TABLE=$(terraform output -raw agent_registry_table_name 2>/dev/null || echo "")
  # Gateway IRSA role ARN lives in the platform layer; read it from IAM rather
  # than cross-layer terraform_remote_state. Deterministic given name_prefix.
  GATEWAY_ROLE_ARN=$(aws iam get-role --role-name "adp-${ENVIRONMENT}-role-gateway-service" --query 'Role.Arn' --output text 2>/dev/null || echo "")
  cd "$ROOT_DIR/modules/gateway"
  kubectl create namespace adp-gateway --dry-run=client -o yaml | kubectl apply -f -
  sed -e "s|__AWS_REGION__|${AWS_REGION}|g" \
      -e "s|__ENVIRONMENT__|${ENVIRONMENT}|g" \
      -e "s|__DB_HOST__|${DB_HOST}|g" \
      -e "s|__DB_USER__|${DB_USER}|g" \
      -e "s|__DB_NAME__|${DB_NAME}|g" \
      -e "s|__REDIS_HOST__|${REDIS_HOST:-localhost}|g" \
      -e "s|__REDIS_PORT__|${REDIS_PORT:-6379}|g" \
      -e "s|__COGNITO_USER_POOL_ID__|${COGNITO_USER_POOL_ID}|g" \
      -e "s|__COGNITO_CLIENT_ID__|${COGNITO_CLIENT_ID}|g" \
      -e "s|__COGNITO_DOMAIN__|${COGNITO_DOMAIN}|g" \
      -e "s|__CORS_ALLOWED_ORIGINS__|https://${CF_DOMAIN},http://localhost:5173|g" \
      -e "s|__GATEWAY_BASE_URL__|https://${CF_DOMAIN}|g" \
      -e "s|__CFN_TEMPLATE_BUCKET__|$(terraform output -raw frontend_bucket_name 2>/dev/null || echo '')|g" \
      -e "s|__GATEWAY_ROLE_ARN__|${GATEWAY_ROLE_ARN}|g" \
      -e "s|__CHAT_LOGGING_ENABLED__|false|g" \
      -e "s|__CHAT_LOGGING_BUCKET__||g" \
      -e "s|__CHAT_LOGGING_SCRUB_LEVEL__|off|g" \
      -e "s|__TRUST_APIGW_HEADERS__|true|g" \
      -e "s|__AGENT_REGISTRY_TABLE__|${AGENT_REGISTRY_TABLE}|g" \
      k8s/configmap.yaml | kubectl apply -f -
  for f in k8s/*.yaml; do
    case "$(basename "$f")" in
      configmap.yaml|targetgroupbinding.yaml) continue ;;
      *) kubectl apply -f "$f" -n adp-gateway ;;
    esac
  done
  kubectl set image deployment/bedrockgateway bedrockgateway="${REGISTRY}/adp-gateway:latest" -n adp-gateway 2>/dev/null || true
  kubectl rollout status deployment/bedrockgateway -n adp-gateway --timeout=300s || warn "Rollout not complete"
fi
ok "Gateway deployed"

# =============================================================================
# Step 4b: Discover internal ALB and wire to API Gateway + CloudFront (Bug 3)
# =============================================================================
if [ "$AGENT_FACTORY_ONLY" = false ] && [ "$AGENT_CONTEXT_ONLY" = false ]; then
  step "Step 4b/8: Wire internal ALB to API Gateway and CloudFront"

  # Discover ALB, cache to SSM, export ALB_ARN / ALB_DNS / ALB_SG_IDS.
  # The shared script exits 1 if the ALB is not found after 10 min; in
  # deploy-all.sh we downgrade that to a warning so the rest of the deploy
  # can continue (API Gateway will keep MOCK integrations).
  if bash "$SCRIPT_DIR/wire-gateway-alb.sh"; then
    # Source the exported variables into this shell (the script also writes
    # to SSM, but we need the values locally for the terraform re-apply).
    ALB_ARN=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/internal-alb-arn" --query "Parameter.Value" --output text --region "$AWS_REGION" 2>/dev/null || echo "")
    ALB_DNS=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/internal-alb-dns" --query "Parameter.Value" --output text --region "$AWS_REGION" 2>/dev/null || echo "")
    ALB_SG_IDS=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/internal-alb-security-group-ids" --query "Parameter.Value" --output text --region "$AWS_REGION" 2>/dev/null || echo "[]")
  else
    warn "ALB not found after 10 minutes. Skipping ALB wiring — API Gateway will use MOCK integration."
    ALB_ARN=""
    ALB_DNS=""
    ALB_SG_IDS="[]"
  fi

  # Re-apply gateway Terraform with ALB details to wire API Gateway VPC Link v2
  # (needs ARN for integration target, DNS for integration URI, SG IDs for egress)
  # and CloudFront VPC Origin (needs ARN only).
  if [ -n "$ALB_ARN" ] && [ "$ALB_ARN" != "None" ] && [ -n "$ALB_DNS" ]; then
    echo "Re-applying gateway Terraform with ALB details to wire API Gateway VPC Link v2 and CloudFront..."
    cd "$ROOT_DIR/modules/gateway/infra"
    terraform apply \
      -var-file="../../../environments/$ENVIRONMENT/modules/gateway.tfvars" \
      -var "internal_alb_arn=$ALB_ARN" \
      -var "internal_alb_dns=$ALB_DNS" \
      -var "alb_security_group_ids=$ALB_SG_IDS" \
      -var "enable_vpc_origin=true" \
      -auto-approve
    ok "API Gateway VPC Link and CloudFront VPC Origin wired to ALB"
  fi
fi

# =============================================================================
# Step 5: Frontend
# =============================================================================
if [ "$SKIP_FRONTEND" = false ] && [ "$AGENT_FACTORY_ONLY" = false ] && [ "$AGENT_CONTEXT_ONLY" = false ]; then
  step "Step 5/6: Deploy frontend"

  # Frontend build runs directly (npm + aws s3 sync) — no CodeBuild needed.
  cd "$ROOT_DIR/modules/gateway/frontend"
  npm ci
  VITE_API_URL="/api" npm run build
  BUCKET=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/frontend-bucket" --query "Parameter.Value" --output text 2>/dev/null) || true
  if [ -n "$BUCKET" ] && [ "$BUCKET" != "None" ]; then
    aws s3 sync dist/ "s3://${BUCKET}/" --delete
    DIST_ID=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/cloudfront-id" --query "Parameter.Value" --output text 2>/dev/null) || true
    [ -n "$DIST_ID" ] && [ "$DIST_ID" != "None" ] && aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/*" > /dev/null
    ok "Frontend deployed"
  else
    warn "Frontend bucket not found in SSM"
  fi
else
  step "Step 5/6: Skipping frontend"
fi

# =============================================================================
# Step 6: Agent Factory
# =============================================================================
if [ "$GATEWAY_ONLY" = false ] && [ "$AGENT_CONTEXT_ONLY" = false ]; then
  step "Step 6/8: Deploy agent-factory"

  # Agent factory infra runs directly — no CodeBuild needed.
  cd "$ROOT_DIR/modules/agent-factory/infra"
  BACKEND_FILE="$ROOT_DIR/environments/$ENVIRONMENT/modules/agent-factory-backend.tfvars"
  [ ! -f "$BACKEND_FILE" ] && cat > "$BACKEND_FILE" << EOF
bucket         = "${STATE_BUCKET}"
key            = "${ENVIRONMENT}/modules/agent-factory/terraform.tfstate"
region         = "${AWS_REGION}"
encrypt        = true
dynamodb_table = "${LOCK_TABLE}"
EOF
  [ ! -f terraform.tfvars ] && cat > terraform.tfvars << EOF
environment      = "${ENVIRONMENT}"
aws_region       = "${AWS_REGION}"
account_id       = "${ACCOUNT_ID}"
github_org       = "${ADP_GITHUB_ORG:-aws-e}"
runner_namespace = "arc-runners"
EOF
  terraform init -backend-config="$BACKEND_FILE" -input=false
  terraform apply -var-file=terraform.tfvars -auto-approve
  ok "Agent-factory deployed"

  # --- Agent Gateway build + deploy (part of agent-factory) ---
  step "Step 7/8: Build and deploy agent gateway"

  # --- Docker build: use CodeBuild (needs privileged mode) or local Docker ---
  LOCAL_IMAGE_TAG="${IMAGE_TAG:-latest}"
  if [ "$LOCAL_MODE" = true ] && docker info &>/dev/null 2>&1; then
    cd "$ROOT_DIR/modules/agent-factory"
    BUILD_DIR="/tmp/agent-gateway-build"
    rm -rf "$BUILD_DIR" && mkdir -p "$BUILD_DIR"
    cp -r gateway/app "$BUILD_DIR/app"
    cp -r agent "$BUILD_DIR/agent"
    cp gateway/Dockerfile "$BUILD_DIR/Dockerfile"
    cp gateway/entrypoint.sh "$BUILD_DIR/entrypoint.sh"

    aws ecr describe-repositories --repository-names "adp-agent-gateway" --region "$AWS_REGION" 2>/dev/null || \
      aws ecr create-repository --repository-name "adp-agent-gateway" --region "$AWS_REGION" --no-cli-pager
    aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY"
    docker build -t "$REGISTRY/adp-agent-gateway:$LOCAL_IMAGE_TAG" "$BUILD_DIR"
    docker tag "$REGISTRY/adp-agent-gateway:$LOCAL_IMAGE_TAG" "$REGISTRY/adp-agent-gateway:latest"
    docker push "$REGISTRY/adp-agent-gateway:$LOCAL_IMAGE_TAG"
    docker push "$REGISTRY/adp-agent-gateway:latest"
    rm -rf "$BUILD_DIR"
  else
    # Docker build via CodeBuild (Terraform-managed project)
    upload_source
    run_codebuild "adp-${ENVIRONMENT}-agent-gateway" "codebuild/bs-agent-gateway.yml"
  fi

  # --- K8s deploy: runs directly (no CodeBuild needed) ---
  cd "$ROOT_DIR/modules/agent-factory"
  kubectl create namespace adp-gateway-agents --dry-run=client -o yaml | kubectl apply -f -
  INPUT_QUEUE_URL=$(cd infra && terraform output -raw gateway_input_queue_url 2>/dev/null || echo "PENDING")
  RESPONSE_QUEUE_URL=$(cd infra && terraform output -raw gateway_response_queue_url 2>/dev/null || echo "PENDING")
  SESSIONS_TABLE=$(cd infra && terraform output -raw gateway_sessions_table 2>/dev/null || echo "PENDING")
  AGENT_IMAGE="$REGISTRY/adp-agent-gateway:$LOCAL_IMAGE_TAG"
  sed -e "s|REPLACE_WITH_INPUT_QUEUE_URL|${INPUT_QUEUE_URL}|g" \
      -e "s|REPLACE_WITH_RESPONSE_QUEUE_URL|${RESPONSE_QUEUE_URL}|g" \
      -e "s|REPLACE_WITH_SESSIONS_TABLE_NAME|${SESSIONS_TABLE}|g" \
      -e "s|REPLACE_WITH_AGENT_IMAGE|${AGENT_IMAGE}|g" \
      gateway/k8s/keda-scaledjob.yaml | kubectl apply -f -
  ok "Agent gateway deployed"

  warn "Store GitHub App creds in Secrets Manager (see modules/agent-factory/SETUP-GUIDE.md)"
else
  step "Step 6/8: Skipping agent-factory"
fi

# =============================================================================
# Step 8: Agent Context (optional — gated by AGENT_CONTEXT_ENABLED or --agent-context-only)
# =============================================================================
DEPLOY_AGENT_CONTEXT=false
if [ "$AGENT_CONTEXT_ONLY" = true ]; then
  DEPLOY_AGENT_CONTEXT=true
elif [ "$GATEWAY_ONLY" = true ] || [ "$AGENT_FACTORY_ONLY" = true ] || [ "$SKIP_AGENT_CONTEXT" = true ]; then
  DEPLOY_AGENT_CONTEXT=false
elif [ "$AGENT_CONTEXT_ENABLED" = true ]; then
  DEPLOY_AGENT_CONTEXT=true
fi

if [ "$DEPLOY_AGENT_CONTEXT" = true ]; then
  step "Step 8/8: Deploy agent-context"

  # Agent context runs directly — no CodeBuild needed.
  cd "$ROOT_DIR/modules/agent-context/terraform"
  BACKEND_FILE="$ROOT_DIR/environments/$ENVIRONMENT/modules/agent-context-backend.tfvars"
  [ ! -f "$BACKEND_FILE" ] && cat > "$BACKEND_FILE" << EOF
bucket         = "${STATE_BUCKET}"
key            = "${ENVIRONMENT}/modules/agent-context/terraform.tfstate"
region         = "${AWS_REGION}"
encrypt        = true
dynamodb_table = "${LOCK_TABLE}"
EOF
  terraform init -backend-config="$BACKEND_FILE" -input=false
  terraform apply -var-file="$ROOT_DIR/environments/$ENVIRONMENT/modules/agent-context.tfvars" -auto-approve
  ok "Agent-context infrastructure deployed"

  # Deploy k8s manifests
  cd "$ROOT_DIR/modules/agent-context"
  bash deploy.sh --skip-validate
  ok "Agent-context deployed"
else
  step "Step 8/8: Skipping agent-context (set AGENT_CONTEXT_ENABLED=true or use --agent-context-only)"
fi

# =============================================================================
# Summary
# =============================================================================
step "Deployment complete"

echo "Platform:  $EKS_CLUSTER"
echo "Gateway:   kubectl get pods -n adp-gateway (configure kubectl: aws eks update-kubeconfig --name $EKS_CLUSTER --region $AWS_REGION)"

CF_DOMAIN=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/cloudfront-domain" --query "Parameter.Value" --output text 2>/dev/null) || true
[ -n "$CF_DOMAIN" ] && [ "$CF_DOMAIN" != "None" ] && echo "Frontend:  https://${CF_DOMAIN}" && echo "API:       https://${CF_DOMAIN}/api/health"
[ "$GATEWAY_ONLY" = false ] && echo "Agents:    kubectl get pods -n arc-runners"
[ "$DEPLOY_AGENT_CONTEXT" = true ] && echo "Context:   kubectl get pods -n agent-context"
GW_WS=$(cd "$ROOT_DIR/modules/agent-factory/infra" && terraform output -raw gateway_ws_endpoint 2>/dev/null) || true
[ -n "$GW_WS" ] && [ "$GW_WS" != "" ] && echo "Gateway:   $GW_WS"
echo ""
echo "To destroy: $0 --destroy"
