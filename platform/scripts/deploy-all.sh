#!/bin/bash
set -euo pipefail

# =============================================================================
# ADP — Deploy Everything
# =============================================================================
# Single script to deploy the entire platform from scratch.
# Usage:
#   ./platform/scripts/deploy-all.sh                    # Deploy all modules
#   ./platform/scripts/deploy-all.sh --gateway-only     # Deploy platform + gateway only
#   ./platform/scripts/deploy-all.sh --destroy          # Tear down everything
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

AWS_REGION="${AWS_REGION:-us-east-1}"
ENVIRONMENT="${ENVIRONMENT:-dev}"
GATEWAY_ONLY=false
DESTROY=false
SKIP_FRONTEND=false
USE_CI_IMAGE=false

# Parse args
for arg in "$@"; do
  case $arg in
    --gateway-only) GATEWAY_ONLY=true ;;
    --destroy) DESTROY=true ;;
    --skip-frontend) SKIP_FRONTEND=true ;;
    --use-ci-image) USE_CI_IMAGE=true ;;
    --help)
      echo "Usage: $0 [--gateway-only] [--skip-frontend] [--use-ci-image] [--destroy]"
      echo ""
      echo "  --gateway-only    Deploy platform + gateway only (skip agent-factory)"
      echo "  --skip-frontend   Skip frontend build and S3 deploy"
      echo "  --use-ci-image    Skip local Docker build, use latest image from ECR (built by CI)"
      echo "  --destroy         Tear down all infrastructure (reverse order)"
      echo ""
      echo "Environment variables:"
      echo "  AWS_REGION        AWS region (default: us-east-1)"
      echo "  ENVIRONMENT       Environment name (default: dev)"
      exit 0
      ;;
  esac
done

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

step() { echo -e "\n${BLUE}━━━ $1 ━━━${NC}\n"; }
ok()   { echo -e "${GREEN}✓ $1${NC}"; }
warn() { echo -e "${YELLOW}⚠ $1${NC}"; }
fail() { echo -e "${RED}✗ $1${NC}"; exit 1; }

# =============================================================================
# Preflight checks
# =============================================================================
step "Preflight checks"

command -v aws >/dev/null 2>&1 || fail "aws CLI not found"
command -v terraform >/dev/null 2>&1 || fail "terraform not found"
command -v kubectl >/dev/null 2>&1 || fail "kubectl not found"
command -v docker >/dev/null 2>&1 || fail "docker not found"
command -v node >/dev/null 2>&1 || fail "node not found"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) || fail "AWS CLI not configured. Run: aws configure"
ok "AWS Account: $ACCOUNT_ID"
ok "Region: $AWS_REGION"
ok "Environment: $ENVIRONMENT"

REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

# =============================================================================
# DESTROY mode
# =============================================================================
if [ "$DESTROY" = true ]; then
  step "Destroying all infrastructure (reverse order)"

  echo "This will destroy ALL ADP infrastructure in $ENVIRONMENT."
  echo "Type 'yes' to confirm:"
  read -r confirm
  [ "$confirm" = "yes" ] || { echo "Aborted."; exit 0; }

  # Agent Factory
  if [ -f "$ROOT_DIR/modules/agent-factory/infra/terraform.tfvars" ]; then
    step "Destroying agent-factory infra"
    cd "$ROOT_DIR/modules/agent-factory/infra"
    terraform destroy -var-file=terraform.tfvars -auto-approve || warn "Agent-factory destroy had errors"
  fi

  # Gateway
  step "Destroying gateway infra"
  cd "$ROOT_DIR/modules/gateway/infra"
  terraform destroy \
    -var-file="../../../environments/$ENVIRONMENT/modules/gateway.tfvars" \
    -auto-approve || warn "Gateway destroy had errors"

  # Platform
  step "Destroying platform infra"
  cd "$ROOT_DIR/platform/infra"
  terraform destroy \
    -var-file="../../environments/$ENVIRONMENT/platform.tfvars" \
    -auto-approve || warn "Platform destroy had errors"

  ok "Infrastructure destroyed. State backend (S3 + DynamoDB) must be deleted manually."
  exit 0
fi

# =============================================================================
# Step 1: Bootstrap Terraform state backend
# =============================================================================
step "Step 1/6: Bootstrap Terraform state backend"

STATE_BUCKET="adp-terraform-state-${ACCOUNT_ID}"
LOCK_TABLE="adp-terraform-locks"

if aws s3api head-bucket --bucket "$STATE_BUCKET" 2>/dev/null; then
  ok "State bucket already exists: $STATE_BUCKET"
else
  echo "Creating state bucket and lock table..."
  cd "$ROOT_DIR/platform/scripts"
  AWS_REGION="$AWS_REGION" ENVIRONMENT="$ENVIRONMENT" bash bootstrap.sh
  ok "State backend created"
fi

# Ensure ACCOUNT_ID placeholders are replaced
find "$ROOT_DIR/environments/" -name "*.tfvars" -exec grep -l "ACCOUNT_ID" {} \; | while read f; do
  sed -i '' "s/ACCOUNT_ID/${ACCOUNT_ID}/g" "$f" 2>/dev/null || sed -i "s/ACCOUNT_ID/${ACCOUNT_ID}/g" "$f"
done
ok "Environment configs updated"

# =============================================================================
# Step 2: Deploy shared platform
# =============================================================================
step "Step 2/6: Deploy shared platform (VPC, EKS, ECR, IAM)"

cd "$ROOT_DIR/platform/infra"
terraform init -backend-config="../../environments/$ENVIRONMENT/backend.tfvars" -input=false
terraform apply -var-file="../../environments/$ENVIRONMENT/platform.tfvars" -auto-approve

ok "Platform deployed"

# Configure kubectl
EKS_CLUSTER="adp-${ENVIRONMENT}-eks"
aws eks update-kubeconfig --name "$EKS_CLUSTER" --region "$AWS_REGION"
ok "kubectl configured for $EKS_CLUSTER"

# Wait for nodes
echo "Waiting for EKS nodes (Auto Mode may take a few minutes)..."
for i in $(seq 1 30); do
  NODE_COUNT=$(kubectl get nodes --no-headers 2>/dev/null | wc -l | tr -d ' ')
  if [ "$NODE_COUNT" -gt 0 ]; then
    ok "EKS nodes ready ($NODE_COUNT nodes)"
    break
  fi
  sleep 10
done

# =============================================================================
# Step 3: Deploy gateway infrastructure
# =============================================================================
step "Step 3/6: Deploy gateway infrastructure (RDS, Cognito, CloudFront, etc.)"

cd "$ROOT_DIR/modules/gateway/infra"
terraform init \
  -backend-config="../../../environments/$ENVIRONMENT/modules/gateway-backend.tfvars" \
  -input=false
terraform apply \
  -var-file="../../../environments/$ENVIRONMENT/modules/gateway.tfvars" \
  -auto-approve

ok "Gateway infrastructure deployed"

# =============================================================================
# Step 4: Deploy gateway application
# =============================================================================
step "Step 4/6: Deploy gateway application"

cd "$ROOT_DIR/modules/gateway"

if [ "$USE_CI_IMAGE" = true ]; then
  # Use the latest image already in ECR (built by gateway-deploy.yml CI)
  echo "Using latest image from ECR (--use-ci-image)..."
  LATEST_TAG=$(aws ecr describe-images --repository-name adp-gateway --region "$AWS_REGION" \
    --query 'sort_by(imageDetails,&imagePushedAt)[-1].imageTags[0]' --output text 2>/dev/null) || true
  if [ -z "$LATEST_TAG" ] || [ "$LATEST_TAG" = "None" ]; then
    fail "No image found in ECR. Run without --use-ci-image to build locally, or push via CI first."
  fi
  IMAGE_URI="$REGISTRY/adp-gateway:$LATEST_TAG"
  ok "Using ECR image: $IMAGE_URI"
else
  # Build locally and push
  echo "Building gateway container locally..."
  docker build -t adp-gateway .

  echo "Pushing to ECR..."
  aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY"
  docker tag adp-gateway:latest "$REGISTRY/adp-gateway:latest"
  docker push "$REGISTRY/adp-gateway:latest"
  IMAGE_URI="$REGISTRY/adp-gateway:latest"
  ok "Image pushed: $IMAGE_URI"
fi

# Deploy k8s manifests
echo "Deploying to EKS..."
kubectl create namespace adp-gateway --dry-run=client -o yaml | kubectl apply -f -

# Update image in deployment if we have a specific tag
if [ "$IMAGE_URI" != "$REGISTRY/adp-gateway:latest" ]; then
  sed "s|image:.*|image: $IMAGE_URI|" k8s/deployment.yaml | kubectl apply -f - -n adp-gateway
  kubectl apply -f k8s/configmap.yaml -f k8s/service.yaml -f k8s/ingress.yaml -f k8s/namespace.yaml -n adp-gateway 2>/dev/null || true
else
  kubectl apply -f k8s/ -n adp-gateway
fi
kubectl rollout status deployment/bedrockgateway -n adp-gateway --timeout=300s || warn "Rollout not complete yet"

ok "Gateway backend deployed"

# =============================================================================
# Step 5: Deploy frontend
# =============================================================================
if [ "$SKIP_FRONTEND" = false ]; then
  step "Step 5/6: Deploy frontend"

  cd "$ROOT_DIR/modules/gateway/frontend"
  npm ci
  VITE_API_URL="/api/gateway" npm run build

  BUCKET=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/frontend-bucket" --query "Parameter.Value" --output text 2>/dev/null) || true
  if [ -n "$BUCKET" ] && [ "$BUCKET" != "None" ]; then
    aws s3 sync dist/ "s3://${BUCKET}/" --delete
    DIST_ID=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/cloudfront-id" --query "Parameter.Value" --output text 2>/dev/null) || true
    if [ -n "$DIST_ID" ] && [ "$DIST_ID" != "None" ]; then
      aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/*" >/dev/null
    fi
    ok "Frontend deployed"
  else
    warn "SSM parameter for frontend bucket not found. Deploy frontend manually."
  fi
else
  step "Step 5/6: Skipping frontend (--skip-frontend)"
fi

# =============================================================================
# Step 6: Deploy agent-factory (optional)
# =============================================================================
if [ "$GATEWAY_ONLY" = false ]; then
  step "Step 6/6: Deploy agent-factory infrastructure"

  cd "$ROOT_DIR/modules/agent-factory/infra"

  # Create backend config if missing
  BACKEND_FILE="$ROOT_DIR/environments/$ENVIRONMENT/modules/agent-factory-backend.tfvars"
  if [ ! -f "$BACKEND_FILE" ]; then
    cat > "$BACKEND_FILE" << EOF
bucket         = "${STATE_BUCKET}"
key            = "${ENVIRONMENT}/modules/agent-factory/terraform.tfstate"
region         = "${AWS_REGION}"
encrypt        = true
dynamodb_table = "${LOCK_TABLE}"
EOF
    ok "Created agent-factory backend config"
  fi

  # Create tfvars if missing
  if [ ! -f terraform.tfvars ]; then
    cat > terraform.tfvars << EOF
environment      = "${ENVIRONMENT}"
aws_region       = "${AWS_REGION}"
account_id       = "${ACCOUNT_ID}"
github_org       = "aws-e"
runner_namespace = "arc-runners"
EOF
    ok "Created agent-factory tfvars"
  fi

  terraform init -backend-config="$BACKEND_FILE" -input=false
  terraform apply -var-file=terraform.tfvars -auto-approve

  ok "Agent-factory infrastructure deployed"
  warn "Remember to store GitHub App credentials in Secrets Manager (see modules/agent-factory/SETUP-GUIDE.md)"
else
  step "Step 6/6: Skipping agent-factory (--gateway-only)"
fi

# =============================================================================
# Summary
# =============================================================================
step "Deployment complete"

echo "Platform:  adp-${ENVIRONMENT}-eks (EKS cluster)"
echo "Gateway:   kubectl get pods -n adp-gateway"

CF_DOMAIN=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/cloudfront-domain" --query "Parameter.Value" --output text 2>/dev/null) || true
if [ -n "$CF_DOMAIN" ] && [ "$CF_DOMAIN" != "None" ]; then
  echo "Frontend:  https://${CF_DOMAIN}"
  echo "API:       https://${CF_DOMAIN}/api/health"
fi

if [ "$GATEWAY_ONLY" = false ]; then
  echo "Agents:    kubectl get pods -n arc-runners"
fi

echo ""
echo "To destroy: $0 --destroy"
