#!/bin/bash
set -euo pipefail

# =============================================================================
# ADP — Deploy Everything (AWS-native)
# =============================================================================
# Only requires: AWS CLI, Terraform, kubectl on the deployer's machine.
# Docker builds and frontend builds happen in AWS via CodeBuild.
#
# Usage:
#   ./platform/scripts/deploy-all.sh                    # Deploy all (builds in AWS)
#   ./platform/scripts/deploy-all.sh --gateway-only     # Platform + gateway only
#   ./platform/scripts/deploy-all.sh --local-build      # Build Docker/frontend locally
#   ./platform/scripts/deploy-all.sh --destroy          # Tear down everything
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

AWS_REGION="${AWS_REGION:-us-east-1}"
ENVIRONMENT="${ENVIRONMENT:-dev}"
GATEWAY_ONLY=false
DESTROY=false
SKIP_FRONTEND=false
LOCAL_BUILD=false

for arg in "$@"; do
  case $arg in
    --gateway-only) GATEWAY_ONLY=true ;;
    --destroy) DESTROY=true ;;
    --skip-frontend) SKIP_FRONTEND=true ;;
    --local-build) LOCAL_BUILD=true ;;
    --help)
      echo "Usage: $0 [--gateway-only] [--skip-frontend] [--local-build] [--destroy]"
      echo ""
      echo "  --gateway-only    Deploy platform + gateway only (skip agent-factory)"
      echo "  --skip-frontend   Skip frontend build and deploy"
      echo "  --local-build     Build Docker image and frontend locally instead of CodeBuild"
      echo "  --destroy         Tear down all infrastructure (reverse order)"
      echo ""
      echo "Default mode requires only: AWS CLI, Terraform, kubectl"
      echo "--local-build also requires: Docker, Node.js >= 22"
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

command -v aws >/dev/null 2>&1 || fail "aws CLI not found"
command -v terraform >/dev/null 2>&1 || fail "terraform not found"
command -v kubectl >/dev/null 2>&1 || fail "kubectl not found"

if [ "$LOCAL_BUILD" = true ]; then
  command -v docker >/dev/null 2>&1 || fail "docker not found (required for --local-build)"
  command -v node >/dev/null 2>&1 || fail "node not found (required for --local-build)"
fi

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) || fail "AWS CLI not configured"
ok "AWS Account: $ACCOUNT_ID | Region: $AWS_REGION | Env: $ENVIRONMENT"

REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
STATE_BUCKET="adp-terraform-state-${ACCOUNT_ID}"
LOCK_TABLE="adp-terraform-locks"
EKS_CLUSTER="adp-${ENVIRONMENT}-eks"

# =============================================================================
# Helper: ensure CodeBuild service role exists
# =============================================================================
ensure_codebuild_role() {
  local ROLE_NAME="adp-${ENVIRONMENT}-codebuild-role"
  if aws iam get-role --role-name "$ROLE_NAME" --query 'Role.Arn' --output text 2>/dev/null; then
    return
  fi
  echo "Creating CodeBuild service role..."
  aws iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document '{
      "Version":"2012-10-17",
      "Statement":[{"Effect":"Allow","Principal":{"Service":"codebuild.amazonaws.com"},"Action":"sts:AssumeRole"}]
    }' > /dev/null
  aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name codebuild-policy \
    --policy-document "{
      \"Version\":\"2012-10-17\",
      \"Statement\":[
        {\"Effect\":\"Allow\",\"Action\":[\"logs:*\"],\"Resource\":\"*\"},
        {\"Effect\":\"Allow\",\"Action\":[\"ecr:*\"],\"Resource\":\"*\"},
        {\"Effect\":\"Allow\",\"Action\":[\"s3:*\"],\"Resource\":[\"arn:aws:s3:::${STATE_BUCKET}\",\"arn:aws:s3:::${STATE_BUCKET}/*\"]},
        {\"Effect\":\"Allow\",\"Action\":[\"s3:PutObject\",\"s3:DeleteObject\",\"s3:GetObject\",\"s3:ListBucket\"],\"Resource\":\"*\"},
        {\"Effect\":\"Allow\",\"Action\":[\"sts:GetCallerIdentity\"],\"Resource\":\"*\"}
      ]
    }" > /dev/null
  sleep 10
  ok "CodeBuild role created: $ROLE_NAME"
}

# =============================================================================
# Helper: run a CodeBuild build from local source
# =============================================================================
run_codebuild() {
  local PROJECT_NAME="$1"
  local BUILDSPEC="$2"
  local SOURCE_ZIP="$3"
  local ENV_VARS="$4"

  ensure_codebuild_role
  local CB_ROLE_ARN
  CB_ROLE_ARN=$(aws iam get-role --role-name "adp-${ENVIRONMENT}-codebuild-role" --query 'Role.Arn' --output text)

  # Upload source
  local S3_KEY="codebuild/${PROJECT_NAME}-source.zip"
  aws s3 cp "$SOURCE_ZIP" "s3://${STATE_BUCKET}/${S3_KEY}" --region "$AWS_REGION" > /dev/null

  # Create or update project
  if aws codebuild batch-get-projects --names "$PROJECT_NAME" --region "$AWS_REGION" \
      --query 'projects[0].name' --output text 2>/dev/null | grep -q "$PROJECT_NAME"; then
    aws codebuild update-project --region "$AWS_REGION" --name "$PROJECT_NAME" \
      --source "{\"type\":\"S3\",\"location\":\"${STATE_BUCKET}/${S3_KEY}\",\"buildspec\":$(echo "$BUILDSPEC" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))')}" \
      --environment "{\"type\":\"LINUX_CONTAINER\",\"image\":\"aws/codebuild/amazonlinux2-x86_64-standard:5.0\",\"computeType\":\"BUILD_GENERAL1_MEDIUM\",\"privilegedMode\":true,\"environmentVariables\":$ENV_VARS}" > /dev/null
  else
    aws codebuild create-project --region "$AWS_REGION" --name "$PROJECT_NAME" \
      --source "{\"type\":\"S3\",\"location\":\"${STATE_BUCKET}/${S3_KEY}\",\"buildspec\":$(echo "$BUILDSPEC" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))')}" \
      --artifacts '{"type":"NO_ARTIFACTS"}' \
      --environment "{\"type\":\"LINUX_CONTAINER\",\"image\":\"aws/codebuild/amazonlinux2-x86_64-standard:5.0\",\"computeType\":\"BUILD_GENERAL1_MEDIUM\",\"privilegedMode\":true,\"environmentVariables\":$ENV_VARS}" \
      --service-role "$CB_ROLE_ARN" > /dev/null
  fi

  # Start and wait
  local BUILD_ID
  BUILD_ID=$(aws codebuild start-build --region "$AWS_REGION" --project-name "$PROJECT_NAME" \
    --query 'build.id' --output text)
  echo "  Build started: $BUILD_ID"

  while true; do
    local STATUS
    STATUS=$(aws codebuild batch-get-builds --ids "$BUILD_ID" --region "$AWS_REGION" \
      --query 'builds[0].buildStatus' --output text)
    case "$STATUS" in
      SUCCEEDED) ok "Build succeeded: $PROJECT_NAME"; return 0 ;;
      FAILED|FAULT|STOPPED|TIMED_OUT) fail "Build failed ($STATUS). Logs: aws codebuild batch-get-builds --ids $BUILD_ID" ;;
      *) sleep 15 ;;
    esac
  done
}

# =============================================================================
# DESTROY
# =============================================================================
if [ "$DESTROY" = true ]; then
  step "Destroying all infrastructure (reverse order)"
  echo "This will destroy ALL ADP infrastructure in $ENVIRONMENT. Type 'yes' to confirm:"
  read -r confirm
  [ "$confirm" = "yes" ] || { echo "Aborted."; exit 0; }

  if [ -f "$ROOT_DIR/modules/agent-factory/infra/terraform.tfvars" ]; then
    step "Destroying agent-factory"
    cd "$ROOT_DIR/modules/agent-factory/infra"
    terraform destroy -var-file=terraform.tfvars -auto-approve || warn "Agent-factory destroy had errors"
  fi

  step "Destroying gateway"
  cd "$ROOT_DIR/modules/gateway/infra"
  terraform destroy -var-file="../../../environments/$ENVIRONMENT/modules/gateway.tfvars" -auto-approve || warn "Gateway destroy had errors"

  for p in "adp-${ENVIRONMENT}-gateway-build" "adp-${ENVIRONMENT}-frontend-build"; do
    aws codebuild delete-project --name "$p" --region "$AWS_REGION" 2>/dev/null || true
  done
  aws iam delete-role-policy --role-name "adp-${ENVIRONMENT}-codebuild-role" --policy-name codebuild-policy 2>/dev/null || true
  aws iam delete-role --role-name "adp-${ENVIRONMENT}-codebuild-role" 2>/dev/null || true

  step "Destroying platform"
  cd "$ROOT_DIR/platform/infra"
  terraform destroy -var-file="../../environments/$ENVIRONMENT/platform.tfvars" -auto-approve || warn "Platform destroy had errors"

  ok "Done. Delete state backend manually: S3 $STATE_BUCKET, DynamoDB $LOCK_TABLE"
  exit 0
fi

# =============================================================================
# Step 1: Bootstrap
# =============================================================================
step "Step 1/6: Bootstrap Terraform state backend"

if aws s3api head-bucket --bucket "$STATE_BUCKET" 2>/dev/null; then
  ok "State bucket exists: $STATE_BUCKET"
else
  cd "$ROOT_DIR/platform/scripts"
  AWS_REGION="$AWS_REGION" ENVIRONMENT="$ENVIRONMENT" bash bootstrap.sh
  ok "State backend created"
fi

find "$ROOT_DIR/environments/" -name "*.tfvars" -exec grep -l "ACCOUNT_ID" {} \; 2>/dev/null | while read f; do
  sed -i '' "s/ACCOUNT_ID/${ACCOUNT_ID}/g" "$f" 2>/dev/null || sed -i "s/ACCOUNT_ID/${ACCOUNT_ID}/g" "$f"
done
ok "Environment configs updated"

# =============================================================================
# Step 2: Shared platform
# =============================================================================
step "Step 2/6: Deploy shared platform (VPC, EKS, ECR, IAM)"

cd "$ROOT_DIR/platform/infra"
terraform init -backend-config="../../environments/$ENVIRONMENT/backend.tfvars" -input=false
terraform apply -var-file="../../environments/$ENVIRONMENT/platform.tfvars" -auto-approve
ok "Platform deployed"

aws eks update-kubeconfig --name "$EKS_CLUSTER" --region "$AWS_REGION"
ok "kubectl configured"

echo "Waiting for EKS nodes..."
for i in $(seq 1 30); do
  [ "$(kubectl get nodes --no-headers 2>/dev/null | wc -l | tr -d ' ')" -gt 0 ] && { ok "Nodes ready"; break; }
  sleep 10
done

# =============================================================================
# Step 3: Gateway infrastructure
# =============================================================================
step "Step 3/6: Deploy gateway infrastructure"

cd "$ROOT_DIR/modules/gateway/infra"
terraform init -backend-config="../../../environments/$ENVIRONMENT/modules/gateway-backend.tfvars" -input=false
terraform apply -var-file="../../../environments/$ENVIRONMENT/modules/gateway.tfvars" -auto-approve
ok "Gateway infrastructure deployed"

# =============================================================================
# Step 4: Build + deploy gateway
# =============================================================================
step "Step 4/6: Build and deploy gateway"

if [ "$LOCAL_BUILD" = true ]; then
  cd "$ROOT_DIR/modules/gateway"
  docker build -t adp-gateway .
  aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY"
  docker tag adp-gateway:latest "$REGISTRY/adp-gateway:latest"
  docker push "$REGISTRY/adp-gateway:latest"
  ok "Image pushed (local build)"
else
  cd "$ROOT_DIR"
  echo "Packaging gateway source for CodeBuild..."
  zip -r /tmp/adp-gw-src.zip modules/gateway/Dockerfile modules/gateway/pyproject.toml \
    modules/gateway/src/ modules/gateway/alembic/ modules/gateway/alembic.ini \
    -x '*.pyc' '*__pycache__*' > /dev/null

  BUILDSPEC="version: 0.2
phases:
  pre_build:
    commands:
      - aws ecr get-login-password --region \$AWS_DEFAULT_REGION | docker login --username AWS --password-stdin \$REGISTRY
  build:
    commands:
      - cd modules/gateway
      - docker build -t \$REGISTRY/adp-gateway:latest .
  post_build:
    commands:
      - docker push \$REGISTRY/adp-gateway:latest"

  run_codebuild "adp-${ENVIRONMENT}-gateway-build" "$BUILDSPEC" "/tmp/adp-gw-src.zip" \
    "[{\"name\":\"AWS_DEFAULT_REGION\",\"value\":\"$AWS_REGION\"},{\"name\":\"REGISTRY\",\"value\":\"$REGISTRY\"}]"
  rm -f /tmp/adp-gw-src.zip
fi

# Deploy to EKS
cd "$ROOT_DIR/modules/gateway"
kubectl create namespace adp-gateway --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f k8s/ -n adp-gateway
kubectl rollout status deployment/bedrockgateway -n adp-gateway --timeout=300s || warn "Rollout not complete yet"
ok "Gateway deployed to EKS"

# =============================================================================
# Step 5: Frontend
# =============================================================================
if [ "$SKIP_FRONTEND" = false ]; then
  step "Step 5/6: Deploy frontend"

  BUCKET=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/frontend-bucket" --query "Parameter.Value" --output text 2>/dev/null) || true

  if [ -z "$BUCKET" ] || [ "$BUCKET" = "None" ]; then
    warn "Frontend bucket SSM parameter not found. Skipping."
  elif [ "$LOCAL_BUILD" = true ]; then
    cd "$ROOT_DIR/modules/gateway/frontend"
    npm ci
    VITE_API_URL="/api/gateway" npm run build
    aws s3 sync dist/ "s3://${BUCKET}/" --delete
    ok "Frontend deployed (local build)"
  else
    cd "$ROOT_DIR"
    echo "Packaging frontend source for CodeBuild..."
    zip -r /tmp/adp-fe-src.zip modules/gateway/frontend/ \
      -x '*node_modules*' '*coverage*' '*.tsbuildinfo' > /dev/null

    BUILDSPEC="version: 0.2
phases:
  install:
    runtime-versions:
      nodejs: 22
    commands:
      - cd modules/gateway/frontend
      - npm ci
  build:
    commands:
      - VITE_API_URL=/api/gateway npm run build
  post_build:
    commands:
      - aws s3 sync dist/ s3://\$FRONTEND_BUCKET/ --delete"

    run_codebuild "adp-${ENVIRONMENT}-frontend-build" "$BUILDSPEC" "/tmp/adp-fe-src.zip" \
      "[{\"name\":\"FRONTEND_BUCKET\",\"value\":\"$BUCKET\"}]"
    rm -f /tmp/adp-fe-src.zip
  fi

  DIST_ID=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/cloudfront-id" --query "Parameter.Value" --output text 2>/dev/null) || true
  if [ -n "$DIST_ID" ] && [ "$DIST_ID" != "None" ]; then
    aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/*" > /dev/null
    ok "CloudFront invalidated"
  fi
else
  step "Step 5/6: Skipping frontend"
fi

# =============================================================================
# Step 6: Agent Factory
# =============================================================================
if [ "$GATEWAY_ONLY" = false ]; then
  step "Step 6/6: Deploy agent-factory"

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
github_org       = "aws-e"
runner_namespace = "arc-runners"
EOF

  terraform init -backend-config="$BACKEND_FILE" -input=false
  terraform apply -var-file=terraform.tfvars -auto-approve
  ok "Agent-factory deployed"
  warn "Store GitHub App creds in Secrets Manager (see modules/agent-factory/SETUP-GUIDE.md)"
else
  step "Step 6/6: Skipping agent-factory"
fi

# =============================================================================
# Summary
# =============================================================================
step "Deployment complete"

echo "Platform:  $EKS_CLUSTER"
echo "Gateway:   kubectl get pods -n adp-gateway"

CF_DOMAIN=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/cloudfront-domain" --query "Parameter.Value" --output text 2>/dev/null) || true
[ -n "$CF_DOMAIN" ] && [ "$CF_DOMAIN" != "None" ] && echo "Frontend:  https://${CF_DOMAIN}" && echo "API:       https://${CF_DOMAIN}/api/health"
[ "$GATEWAY_ONLY" = false ] && echo "Agents:    kubectl get pods -n arc-runners"
echo ""
echo "To destroy: $0 --destroy"
