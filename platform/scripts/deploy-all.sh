#!/bin/bash
set -euo pipefail

# =============================================================================
# ADP — Deploy Everything (fully AWS-native)
# =============================================================================
# Deploys the entire platform using only AWS services.
# The deployer only needs: AWS CLI (configured with admin access).
# Terraform, Docker, Node.js all run in CodeBuild.
#
# Usage:
#   ./platform/scripts/deploy-all.sh                    # Deploy all modules
#   ./platform/scripts/deploy-all.sh --gateway-only     # Platform + gateway only
#   ./platform/scripts/deploy-all.sh --destroy          # Tear down everything
#   ./platform/scripts/deploy-all.sh --local            # Run everything locally (needs Terraform, Docker, Node, kubectl)
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

AWS_REGION="${AWS_REGION:-us-east-1}"
ENVIRONMENT="${ENVIRONMENT:-dev}"
GATEWAY_ONLY=false
AGENT_FACTORY_ONLY=false
DESTROY=false
SKIP_FRONTEND=false
LOCAL_MODE=false

for arg in "$@"; do
  case $arg in
    --gateway-only) GATEWAY_ONLY=true ;;
    --agent-factory-only) AGENT_FACTORY_ONLY=true ;;
    --destroy) DESTROY=true ;;
    --skip-frontend) SKIP_FRONTEND=true ;;
    --local) LOCAL_MODE=true ;;
    --help)
      echo "Usage: $0 [--gateway-only] [--agent-factory-only] [--skip-frontend] [--local] [--destroy]"
      echo ""
      echo "  (default)              Deploy all modules (platform + gateway + agent-factory)"
      echo "  --gateway-only         Deploy platform + gateway only (skip agent-factory)"
      echo "  --agent-factory-only   Deploy platform + agent-factory only (skip gateway)"
      echo "  --skip-frontend        Skip frontend build and deploy"
      echo "  --local                Run Terraform/Docker/npm locally"
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
# Helper: ensure CodeBuild IAM role
# =============================================================================
ensure_codebuild_role() {
  if aws iam get-role --role-name "$CB_ROLE_NAME" 2>/dev/null > /dev/null; then return; fi
  echo "Creating CodeBuild service role..."
  aws iam create-role --role-name "$CB_ROLE_NAME" \
    --assume-role-policy-document '{
      "Version":"2012-10-17",
      "Statement":[{"Effect":"Allow","Principal":{"Service":"codebuild.amazonaws.com"},"Action":"sts:AssumeRole"}]
    }' > /dev/null
  # Admin-like policy for Terraform to create any resource
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
# Helper: run a CodeBuild job with a buildspec
# =============================================================================
run_codebuild() {
  local PROJECT_NAME="$1"
  local BUILDSPEC_FILE="$2"

  ensure_codebuild_role
  local CB_ROLE_ARN
  CB_ROLE_ARN=$(aws iam get-role --role-name "$CB_ROLE_NAME" --query 'Role.Arn' --output text)

  # Create or update project
  local SOURCE_LOC="${STATE_BUCKET}/codebuild/adp-source.zip"
  local PROJECT_EXISTS
  PROJECT_EXISTS=$(aws codebuild batch-get-projects --names "$PROJECT_NAME" --region "$AWS_REGION" \
    --query 'projects | length(@)' --output text 2>/dev/null || echo "0")

  local CMD="create-project"
  [ "$PROJECT_EXISTS" -gt 0 ] && CMD="update-project"

  aws codebuild $CMD --region "$AWS_REGION" \
    --name "$PROJECT_NAME" \
    --source "{\"type\":\"S3\",\"location\":\"$SOURCE_LOC\",\"buildspec\":\"$BUILDSPEC_FILE\"}" \
    --artifacts '{"type":"NO_ARTIFACTS"}' \
    --environment '{
      "type":"LINUX_CONTAINER",
      "image":"aws/codebuild/amazonlinux2-x86_64-standard:5.0",
      "computeType":"BUILD_GENERAL1_MEDIUM",
      "privilegedMode":true
    }' \
    --service-role "$CB_ROLE_ARN" > /dev/null 2>&1

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
# DESTROY
# =============================================================================
if [ "$DESTROY" = true ]; then
  step "Destroying all infrastructure"
  echo "This will destroy ALL ADP infrastructure in $ENVIRONMENT. Type 'yes':"
  read -r confirm
  [ "$confirm" = "yes" ] || { echo "Aborted."; exit 0; }

  if [ "$LOCAL_MODE" = true ]; then
    [ -f "$ROOT_DIR/modules/agent-factory/infra/terraform.tfvars" ] && {
      cd "$ROOT_DIR/modules/agent-factory/infra"
      terraform destroy -var-file=terraform.tfvars -auto-approve || true
    }
    cd "$ROOT_DIR/modules/gateway/infra"
    terraform destroy -var-file="../../../environments/$ENVIRONMENT/modules/gateway.tfvars" -auto-approve || true
    cd "$ROOT_DIR/platform/infra"
    terraform destroy -var-file="../../environments/$ENVIRONMENT/platform.tfvars" -auto-approve || true
  else
    upload_source
    # Write a destroy buildspec inline
    cat > /tmp/adp-destroy-buildspec.yml << 'BSEOF'
version: 0.2
env:
  variables:
    TF_IN_AUTOMATION: "true"
phases:
  install:
    commands:
      - yum install -y yum-utils
      - yum-config-manager --add-repo https://rpm.releases.hashicorp.com/AmazonLinux/hashicorp.repo
      - yum install -y terraform
      - curl -LO "https://dl.k8s.io/release/$(curl -sL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" && chmod +x kubectl && mv kubectl /usr/local/bin/
  build:
    commands:
      - echo "Destroying agent-factory..."
      - |
        if [ -f modules/agent-factory/infra/terraform.tfvars ]; then
          cd modules/agent-factory/infra
          terraform init -backend-config="../../../environments/$ENVIRONMENT/modules/agent-factory-backend.tfvars" -input=false || true
          terraform destroy -var-file=terraform.tfvars -auto-approve || true
          cd ../../..
        fi
      - echo "Destroying gateway..."
      - cd modules/gateway/infra
      - terraform init -backend-config="../../../environments/$ENVIRONMENT/modules/gateway-backend.tfvars" -input=false || true
      - terraform destroy -var-file="../../../environments/$ENVIRONMENT/modules/gateway.tfvars" -auto-approve || true
      - cd ../../..
      - echo "Destroying platform..."
      - cd platform/infra
      - terraform init -backend-config="../../environments/$ENVIRONMENT/backend.tfvars" -input=false || true
      - terraform destroy -var-file="../../environments/$ENVIRONMENT/platform.tfvars" -auto-approve || true
BSEOF
    aws s3 cp /tmp/adp-destroy-buildspec.yml "s3://${STATE_BUCKET}/codebuild/destroy-buildspec.yml" --region "$AWS_REGION" > /dev/null
    run_codebuild "adp-${ENVIRONMENT}-destroy" "codebuild/destroy-buildspec.yml"
  fi

  # Clean up CodeBuild resources
  for p in "adp-${ENVIRONMENT}-platform-infra" "adp-${ENVIRONMENT}-gateway-infra" "adp-${ENVIRONMENT}-gateway-build" "adp-${ENVIRONMENT}-frontend-build" "adp-${ENVIRONMENT}-agent-factory-infra" "adp-${ENVIRONMENT}-destroy"; do
    aws codebuild delete-project --name "$p" --region "$AWS_REGION" 2>/dev/null || true
  done
  aws iam detach-role-policy --role-name "$CB_ROLE_NAME" --policy-arn "arn:aws:iam::aws:policy/AdministratorAccess" 2>/dev/null || true
  aws iam delete-role --role-name "$CB_ROLE_NAME" 2>/dev/null || true

  ok "Done. Delete state backend manually: S3 $STATE_BUCKET, DynamoDB $LOCK_TABLE"
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

# Upload source for CodeBuild steps
if [ "$LOCAL_MODE" = false ]; then
  upload_source
fi

# =============================================================================
# Write buildspec files to S3 (used by CodeBuild)
# =============================================================================
if [ "$LOCAL_MODE" = false ]; then
  step "Uploading buildspec files"

  # --- Platform infra buildspec ---
  cat > /tmp/bs-platform.yml << 'EOF'
version: 0.2
env:
  variables:
    TF_IN_AUTOMATION: "true"
phases:
  install:
    commands:
      - yum install -y yum-utils
      - yum-config-manager --add-repo https://rpm.releases.hashicorp.com/AmazonLinux/hashicorp.repo
      - yum install -y terraform
  build:
    commands:
      - cd platform/infra
      - terraform init -backend-config="../../environments/$ENVIRONMENT/backend.tfvars" -input=false
      - terraform apply -var-file="../../environments/$ENVIRONMENT/platform.tfvars" -auto-approve
EOF

  # --- Gateway infra buildspec ---
  cat > /tmp/bs-gateway-infra.yml << 'EOF'
version: 0.2
env:
  variables:
    TF_IN_AUTOMATION: "true"
phases:
  install:
    commands:
      - yum install -y yum-utils
      - yum-config-manager --add-repo https://rpm.releases.hashicorp.com/AmazonLinux/hashicorp.repo
      - yum install -y terraform
  build:
    commands:
      - cd modules/gateway/infra
      - terraform init -backend-config="../../../environments/$ENVIRONMENT/modules/gateway-backend.tfvars" -input=false
      - terraform apply -var-file="../../../environments/$ENVIRONMENT/modules/gateway.tfvars" -auto-approve
EOF

  # --- Gateway Docker build ---
  cat > /tmp/bs-gateway-build.yml << 'EOF'
version: 0.2
phases:
  pre_build:
    commands:
      - aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $REGISTRY
  build:
    commands:
      - cd modules/gateway
      - docker build -t $REGISTRY/adp-gateway:latest .
      - '[ -n "${IMAGE_TAG:-}" ] && docker tag $REGISTRY/adp-gateway:latest $REGISTRY/adp-gateway:$IMAGE_TAG || true'
  post_build:
    commands:
      - docker push $REGISTRY/adp-gateway:latest
      - '[ -n "${IMAGE_TAG:-}" ] && docker push $REGISTRY/adp-gateway:$IMAGE_TAG || true'
EOF

  # --- Frontend build ---
  cat > /tmp/bs-frontend.yml << 'EOF'
version: 0.2
phases:
  install:
    runtime-versions:
      nodejs: 22
    commands:
      - cd modules/gateway/frontend
      - npm ci
  build:
    commands:
      - cd modules/gateway/frontend
      - VITE_API_URL=/api/gateway npm run build
  post_build:
    commands:
      - BUCKET=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/frontend-bucket" --query "Parameter.Value" --output text)
      - cd modules/gateway/frontend
      - aws s3 sync dist/ "s3://$BUCKET/" --delete
      - DIST_ID=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/cloudfront-id" --query "Parameter.Value" --output text) || true
      - '[ -n "$DIST_ID" ] && [ "$DIST_ID" != "None" ] && aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/*" || true'
EOF

  # --- Gateway k8s deploy ---
  cat > /tmp/bs-gateway-deploy.yml << 'EOF'
version: 0.2
env:
  variables:
    TF_IN_AUTOMATION: "true"
phases:
  install:
    commands:
      - curl -LO "https://dl.k8s.io/release/$(curl -sL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" && chmod +x kubectl && mv kubectl /usr/local/bin/
      - yum install -y yum-utils
      - yum-config-manager --add-repo https://rpm.releases.hashicorp.com/AmazonLinux/hashicorp.repo
      - yum install -y terraform
      - aws eks update-kubeconfig --name $EKS_CLUSTER --region $AWS_REGION
  pre_build:
    commands:
      # Read gateway Terraform outputs for configmap population
      - cd modules/gateway/infra
      - terraform init -backend-config="../../../environments/$ENVIRONMENT/modules/gateway-backend.tfvars" -input=false -reconfigure 2>/dev/null || true
      - export DB_HOST=$(terraform output -raw rds_endpoint 2>/dev/null | sed 's/:5432//' || echo "localhost")
      - export DB_NAME=$(terraform output -raw rds_database_name 2>/dev/null || echo "bedrockgateway")
      - export DB_USER="bgadmin"
      - export REDIS_HOST=$(terraform output -raw redis_endpoint 2>/dev/null || echo "localhost")
      - export REDIS_PORT=$(terraform output -raw redis_port 2>/dev/null || echo "6379")
      - export COGNITO_USER_POOL_ID=$(terraform output -raw cognito_user_pool_id 2>/dev/null || echo "")
      - export COGNITO_CLIENT_ID=$(terraform output -raw cognito_user_pool_client_id 2>/dev/null || echo "")
      - export COGNITO_DOMAIN=$(terraform output -raw cognito_domain 2>/dev/null || echo "")
      - export CF_DOMAIN=$(terraform output -raw frontend_cloudfront_domain_name 2>/dev/null || echo "")
      - export AGENT_REGISTRY_TABLE=$(terraform output -raw agent_registry_table_name 2>/dev/null || echo "")
      - cd ../../..
  build:
    commands:
      - cd modules/gateway
      - kubectl create namespace adp-gateway --dry-run=client -o yaml | kubectl apply -f -
      # Populate configmap placeholders with actual values
      - |
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
            -e "s|__CHAT_LOGGING_ENABLED__|false|g" \
            -e "s|__CHAT_LOGGING_BUCKET__||g" \
            -e "s|__CHAT_LOGGING_SCRUB_LEVEL__|off|g" \
            -e "s|__TRUST_APIGW_HEADERS__|true|g" \
            -e "s|__AGENT_REGISTRY_TABLE__|${AGENT_REGISTRY_TABLE}|g" \
            k8s/configmap.yaml | kubectl apply -f -
      # Apply remaining k8s resources (skip configmap since we just applied it)
      - |
        for f in k8s/*.yaml; do
          case "$(basename $f)" in
            configmap.yaml) continue ;;
            targetgroupbinding.yaml) continue ;;
            *) kubectl apply -f "$f" -n adp-gateway ;;
          esac
        done
      # Update deployment image to use correct registry
      - kubectl set image deployment/bedrockgateway bedrockgateway=${REGISTRY}/adp-gateway:latest -n adp-gateway 2>/dev/null || true
      - kubectl rollout status deployment/bedrockgateway -n adp-gateway --timeout=300s || echo "Rollout still in progress"
EOF

  # --- Agent factory infra ---
  cat > /tmp/bs-agent-factory.yml << 'EOF'
version: 0.2
env:
  variables:
    TF_IN_AUTOMATION: "true"
phases:
  install:
    commands:
      - yum install -y yum-utils
      - yum-config-manager --add-repo https://rpm.releases.hashicorp.com/AmazonLinux/hashicorp.repo
      - yum install -y terraform
  pre_build:
    commands:
      - |
        BACKEND_FILE="environments/$ENVIRONMENT/modules/agent-factory-backend.tfvars"
        if [ ! -f "$BACKEND_FILE" ]; then
          mkdir -p "$(dirname $BACKEND_FILE)"
          cat > "$BACKEND_FILE" << INNER
        bucket         = "$STATE_BUCKET"
        key            = "$ENVIRONMENT/modules/agent-factory/terraform.tfstate"
        region         = "$AWS_REGION"
        encrypt        = true
        dynamodb_table = "adp-terraform-locks"
        INNER
        fi
      - |
        if [ ! -f modules/agent-factory/infra/terraform.tfvars ]; then
          cat > modules/agent-factory/infra/terraform.tfvars << INNER
        environment      = "$ENVIRONMENT"
        aws_region       = "$AWS_REGION"
        account_id       = "$ACCOUNT_ID"
        github_org       = "aws-e"
        runner_namespace = "arc-runners"
        INNER
        fi
  build:
    commands:
      - cd modules/agent-factory/infra
      - terraform init -backend-config="../../../environments/$ENVIRONMENT/modules/agent-factory-backend.tfvars" -input=false
      - terraform apply -var-file=terraform.tfvars -auto-approve
EOF

  # --- Agent factory gateway build + deploy ---
  cat > /tmp/bs-agent-gateway.yml << 'EOF'
version: 0.2
phases:
  install:
    commands:
      - curl -LO "https://dl.k8s.io/release/$(curl -sL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" && chmod +x kubectl && mv kubectl /usr/local/bin/
      - aws eks update-kubeconfig --name $EKS_CLUSTER --region $AWS_REGION
  pre_build:
    commands:
      - REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
      - ECR_REPO="adp-agent-gateway"
      - aws ecr describe-repositories --repository-names "$ECR_REPO" --region "$AWS_REGION" 2>/dev/null || aws ecr create-repository --repository-name "$ECR_REPO" --region "$AWS_REGION"
      - aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY"
  build:
    commands:
      - cd modules/agent-factory
      - BUILD_DIR="/tmp/agent-gateway-build"
      - rm -rf "$BUILD_DIR" && mkdir -p "$BUILD_DIR"
      - cp -r gateway/app "$BUILD_DIR/app"
      - cp -r agent "$BUILD_DIR/agent"
      - cp gateway/Dockerfile "$BUILD_DIR/Dockerfile"
      - cp gateway/entrypoint.sh "$BUILD_DIR/entrypoint.sh"
      - docker build -t "$REGISTRY/$ECR_REPO:latest" "$BUILD_DIR"
      - docker push "$REGISTRY/$ECR_REPO:latest"
      - rm -rf "$BUILD_DIR"
  post_build:
    commands:
      - cd modules/agent-factory/infra
      - terraform init -backend-config="../../../environments/$ENVIRONMENT/modules/agent-factory-backend.tfvars" -input=false -reconfigure 2>/dev/null || true
      - INPUT_QUEUE_URL=$(terraform output -raw gateway_input_queue_url 2>/dev/null || echo "PENDING")
      - RESPONSE_QUEUE_URL=$(terraform output -raw gateway_response_queue_url 2>/dev/null || echo "PENDING")
      - SESSIONS_TABLE=$(terraform output -raw gateway_sessions_table 2>/dev/null || echo "PENDING")
      - REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
      - AGENT_IMAGE="${REGISTRY}/adp-agent-gateway:latest"
      - cd ../
      - kubectl create namespace adp-gateway-agents --dry-run=client -o yaml | kubectl apply -f -
      - |
        sed -e "s|REPLACE_WITH_INPUT_QUEUE_URL|${INPUT_QUEUE_URL}|g" \
            -e "s|REPLACE_WITH_RESPONSE_QUEUE_URL|${RESPONSE_QUEUE_URL}|g" \
            -e "s|REPLACE_WITH_SESSIONS_TABLE_NAME|${SESSIONS_TABLE}|g" \
            -e "s|REPLACE_WITH_AGENT_IMAGE|${AGENT_IMAGE}|g" \
            gateway/k8s/keda-scaledjob.yaml | kubectl apply -f -
      - echo "Agent gateway deployed"
EOF

  # Add buildspec files to the source zip so CodeBuild can find them
  mkdir -p /tmp/adp-buildspecs/codebuild
  for f in bs-platform bs-gateway-infra bs-gateway-build bs-frontend bs-gateway-deploy bs-agent-factory bs-agent-gateway; do
    cp "/tmp/${f}.yml" "/tmp/adp-buildspecs/codebuild/${f}.yml"
    rm -f "/tmp/${f}.yml"
  done
  # Re-upload source with buildspecs included
  cd /tmp/adp-buildspecs
  zip -r /tmp/adp-deploy-source-bs.zip codebuild/ > /dev/null 2>&1
  cd "$ROOT_DIR"
  # Download existing source, append buildspecs, re-upload
  aws s3 cp "s3://${STATE_BUCKET}/codebuild/adp-source.zip" /tmp/adp-source-existing.zip --region "$AWS_REGION" > /dev/null
  cp /tmp/adp-source-existing.zip /tmp/adp-source-final.zip
  cd /tmp/adp-buildspecs && zip -r /tmp/adp-source-final.zip codebuild/ > /dev/null 2>&1
  aws s3 cp /tmp/adp-source-final.zip "s3://${STATE_BUCKET}/codebuild/adp-source.zip" --region "$AWS_REGION" > /dev/null
  rm -rf /tmp/adp-buildspecs /tmp/adp-source-existing.zip /tmp/adp-source-final.zip /tmp/adp-deploy-source-bs.zip
  cd "$ROOT_DIR"
  ok "Buildspec files uploaded"
fi

# =============================================================================
# Step 2: Platform infra
# =============================================================================
step "Step 2/6: Deploy shared platform (VPC, EKS, ECR, IAM)"

if [ "$LOCAL_MODE" = true ]; then
  cd "$ROOT_DIR/platform/infra"
  terraform init -backend-config="../../environments/$ENVIRONMENT/backend.tfvars" -input=false
  terraform apply -var-file="../../environments/$ENVIRONMENT/platform.tfvars" -auto-approve
else
  run_codebuild "adp-${ENVIRONMENT}-platform-infra" "codebuild/bs-platform.yml"
fi
ok "Platform deployed"

# Configure kubectl (needed for k8s steps — local or CodeBuild deploy step)
if command -v kubectl >/dev/null 2>&1; then
  aws eks update-kubeconfig --name "$EKS_CLUSTER" --region "$AWS_REGION" 2>/dev/null || true
fi

# =============================================================================
# Step 3: Gateway infra
# =============================================================================
step "Step 3/6: Deploy gateway infrastructure"

if [ "$AGENT_FACTORY_ONLY" = true ]; then
  echo "Skipping gateway infra (--agent-factory-only)"
  ok "Skipped"
elif [ "$LOCAL_MODE" = true ]; then
  cd "$ROOT_DIR/modules/gateway/infra"
  terraform init -backend-config="../../../environments/$ENVIRONMENT/modules/gateway-backend.tfvars" -input=false
  terraform apply -var-file="../../../environments/$ENVIRONMENT/modules/gateway.tfvars" -auto-approve
  ok "Gateway infrastructure deployed"
else
  run_codebuild "adp-${ENVIRONMENT}-gateway-infra" "codebuild/bs-gateway-infra.yml"
  ok "Gateway infrastructure deployed"
fi

# =============================================================================
# Step 4: Build + deploy gateway
# =============================================================================
step "Step 4/6: Build and deploy gateway"

if [ "$AGENT_FACTORY_ONLY" = true ]; then
  echo "Skipping gateway deploy (--agent-factory-only)"
  ok "Skipped"
elif [ "$LOCAL_MODE" = true ]; then
  # Build Docker image (fall back to CodeBuild if Docker daemon not available)
  if docker info &>/dev/null 2>&1; then
    cd "$ROOT_DIR/modules/gateway"
    docker build -t adp-gateway .
    aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY"
    docker tag adp-gateway:latest "$REGISTRY/adp-gateway:latest"
    docker push "$REGISTRY/adp-gateway:latest"
  else
    warn "Docker daemon not available, using CodeBuild for image build"
    upload_source
    ensure_codebuild_role
    # Write buildspec inline for gateway build
    cat > /tmp/bs-gateway-build-local.yml << 'BSEOF'
version: 0.2
phases:
  pre_build:
    commands:
      - aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $REGISTRY
  build:
    commands:
      - cd modules/gateway
      - docker build -t $REGISTRY/adp-gateway:latest .
  post_build:
    commands:
      - docker push $REGISTRY/adp-gateway:latest
BSEOF
    aws s3 cp /tmp/bs-gateway-build-local.yml "s3://${STATE_BUCKET}/codebuild/bs-gateway-build-local.yml" --region "$AWS_REGION" > /dev/null
    rm -f /tmp/bs-gateway-build-local.yml
    run_codebuild "adp-${ENVIRONMENT}-gateway-build" "codebuild/bs-gateway-build-local.yml"
  fi
  # Deploy to EKS
  cd "$ROOT_DIR/modules/gateway"
  kubectl create namespace adp-gateway --dry-run=client -o yaml | kubectl apply -f -
  # Read terraform outputs for configmap population
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
  cd "$ROOT_DIR/modules/gateway"
  # Apply configmap with populated values
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
      -e "s|__CHAT_LOGGING_ENABLED__|false|g" \
      -e "s|__CHAT_LOGGING_BUCKET__||g" \
      -e "s|__CHAT_LOGGING_SCRUB_LEVEL__|off|g" \
      -e "s|__TRUST_APIGW_HEADERS__|true|g" \
      -e "s|__AGENT_REGISTRY_TABLE__|${AGENT_REGISTRY_TABLE}|g" \
      k8s/configmap.yaml | kubectl apply -f -
  # Apply remaining k8s resources (skip configmap and targetgroupbinding)
  for f in k8s/*.yaml; do
    case "$(basename "$f")" in
      configmap.yaml|targetgroupbinding.yaml) continue ;;
      *) kubectl apply -f "$f" -n adp-gateway ;;
    esac
  done
  # Update deployment image
  kubectl set image deployment/bedrockgateway bedrockgateway="${REGISTRY}/adp-gateway:latest" -n adp-gateway 2>/dev/null || true
  kubectl rollout status deployment/bedrockgateway -n adp-gateway --timeout=300s || warn "Rollout not complete"
else
  run_codebuild "adp-${ENVIRONMENT}-gateway-build" "codebuild/bs-gateway-build.yml"
  run_codebuild "adp-${ENVIRONMENT}-gateway-deploy" "codebuild/bs-gateway-deploy.yml"
fi
ok "Gateway deployed"

# =============================================================================
# Step 4b: Discover internal ALB and wire to API Gateway + CloudFront (Bug 3)
# =============================================================================
if [ "$AGENT_FACTORY_ONLY" = false ]; then
  step "Step 4b/6: Wire internal ALB to API Gateway and CloudFront"

  # Check SSM cache first
  CACHED_ALB_ARN=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/internal-alb-arn" --query "Parameter.Value" --output text --region "$AWS_REGION" 2>/dev/null || echo "pending")

  if [ "$CACHED_ALB_ARN" != "pending" ] && [ -n "$CACHED_ALB_ARN" ]; then
    # Validate cached ALB still exists
    if aws elbv2 describe-load-balancers --load-balancer-arns "$CACHED_ALB_ARN" --region "$AWS_REGION" > /dev/null 2>&1; then
      ALB_ARN="$CACHED_ALB_ARN"
      ALB_DNS=$(aws ssm get-parameter --name "/adp/$ENVIRONMENT/gateway/internal-alb-dns" --query "Parameter.Value" --output text --region "$AWS_REGION" 2>/dev/null || echo "")
      ok "ALB found in SSM cache: $ALB_DNS"
    else
      warn "Cached ALB no longer valid, rediscovering..."
      CACHED_ALB_ARN="pending"
    fi
  fi

  if [ "$CACHED_ALB_ARN" = "pending" ] || [ -z "$CACHED_ALB_ARN" ]; then
    echo "Waiting for EKS Ingress ALB to be provisioned..."
    ALB_ARN=""
    for i in $(seq 1 40); do
      # Look for ALBs tagged with the ingress group
      ALB_ARN=$(aws elbv2 describe-load-balancers --region "$AWS_REGION" \
        --query 'LoadBalancers[?Scheme==`internal`].LoadBalancerArn' --output text 2>/dev/null | head -1 || true)

      if [ -z "$ALB_ARN" ]; then
        # Also check by name pattern from ingress group
        ALB_ARN=$(aws elbv2 describe-load-balancers --region "$AWS_REGION" \
          --query 'LoadBalancers[?contains(LoadBalancerName,`bedrockgw`) || contains(LoadBalancerName,`k8s-bedrockgw`)].LoadBalancerArn' --output text 2>/dev/null | head -1 || true)
      fi

      if [ -n "$ALB_ARN" ] && [ "$ALB_ARN" != "None" ]; then
        ALB_DNS=$(aws elbv2 describe-load-balancers --load-balancer-arns "$ALB_ARN" --region "$AWS_REGION" \
          --query 'LoadBalancers[0].DNSName' --output text 2>/dev/null || echo "")
        ALB_STATE=$(aws elbv2 describe-load-balancers --load-balancer-arns "$ALB_ARN" --region "$AWS_REGION" \
          --query 'LoadBalancers[0].State.Code' --output text 2>/dev/null || echo "")
        if [ "$ALB_STATE" = "active" ]; then
          ok "ALB active: $ALB_DNS"
          break
        fi
        echo "  ALB found but state=$ALB_STATE, waiting..."
      else
        echo "  Attempt $i/40: ALB not yet created, waiting 15s..."
      fi
      sleep 15
    done

    if [ -z "$ALB_ARN" ] || [ "$ALB_ARN" = "None" ]; then
      warn "ALB not found after 10 minutes. Skipping ALB wiring — API Gateway will use MOCK integration."
    else
      # Cache to SSM
      aws ssm put-parameter --name "/adp/$ENVIRONMENT/gateway/internal-alb-arn" --value "$ALB_ARN" --type String --overwrite --region "$AWS_REGION" > /dev/null
      aws ssm put-parameter --name "/adp/$ENVIRONMENT/gateway/internal-alb-dns" --value "$ALB_DNS" --type String --overwrite --region "$AWS_REGION" > /dev/null
      ok "ALB ARN/DNS cached in SSM"
    fi
  fi

  # Re-apply gateway Terraform with ALB ARN to wire VPC Link and CloudFront origin
  if [ -n "$ALB_ARN" ] && [ "$ALB_ARN" != "None" ] && [ -n "$ALB_DNS" ]; then
    echo "Re-applying gateway Terraform with internal_alb_arn to wire API Gateway VPC Link..."
    if [ "$LOCAL_MODE" = true ]; then
      cd "$ROOT_DIR/modules/gateway/infra"
      terraform apply \
        -var-file="../../../environments/$ENVIRONMENT/modules/gateway.tfvars" \
        -var "internal_alb_arn=$ALB_ARN" \
        -var "enable_vpc_origin=true" \
        -auto-approve
    else
      # Write an ALB-wiring buildspec
      cat > /tmp/bs-gateway-alb-wire.yml << BSEOF
version: 0.2
env:
  variables:
    TF_IN_AUTOMATION: "true"
phases:
  install:
    commands:
      - yum install -y yum-utils
      - yum-config-manager --add-repo https://rpm.releases.hashicorp.com/AmazonLinux/hashicorp.repo
      - yum install -y terraform
  build:
    commands:
      - cd modules/gateway/infra
      - terraform init -backend-config="../../../environments/\$ENVIRONMENT/modules/gateway-backend.tfvars" -input=false
      - terraform apply -var-file="../../../environments/\$ENVIRONMENT/modules/gateway.tfvars" -var "internal_alb_arn=${ALB_ARN}" -var "enable_vpc_origin=true" -auto-approve
BSEOF
      aws s3 cp /tmp/bs-gateway-alb-wire.yml "s3://${STATE_BUCKET}/codebuild/bs-gateway-alb-wire.yml" --region "$AWS_REGION" > /dev/null
      rm -f /tmp/bs-gateway-alb-wire.yml
      # Re-upload source since we may have modified files
      upload_source
      run_codebuild "adp-${ENVIRONMENT}-gateway-alb-wire" "codebuild/bs-gateway-alb-wire.yml"
    fi
    ok "API Gateway VPC Link and CloudFront VPC Origin wired to ALB"
  fi
fi

# =============================================================================
# Step 5: Frontend
# =============================================================================
if [ "$SKIP_FRONTEND" = false ] && [ "$AGENT_FACTORY_ONLY" = false ]; then
  step "Step 5/6: Deploy frontend"

  if [ "$LOCAL_MODE" = true ]; then
    cd "$ROOT_DIR/modules/gateway/frontend"
    npm ci
    VITE_API_URL="/api/gateway" npm run build
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
    run_codebuild "adp-${ENVIRONMENT}-frontend-build" "codebuild/bs-frontend.yml"
    ok "Frontend deployed"
  fi
else
  step "Step 5/6: Skipping frontend"
fi

# =============================================================================
# Step 6: Agent Factory
# =============================================================================
if [ "$GATEWAY_ONLY" = false ]; then
  step "Step 6/7: Deploy agent-factory"

  if [ "$LOCAL_MODE" = true ]; then
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
  else
    run_codebuild "adp-${ENVIRONMENT}-agent-factory-infra" "codebuild/bs-agent-factory.yml"
  fi
  ok "Agent-factory deployed"

  # --- Agent Gateway build + deploy (part of agent-factory) ---
  step "Step 7/7: Build and deploy agent gateway"

  if [ "$LOCAL_MODE" = true ]; then
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
    docker build -t "$REGISTRY/adp-agent-gateway:latest" "$BUILD_DIR"
    docker push "$REGISTRY/adp-agent-gateway:latest"
    rm -rf "$BUILD_DIR"

    kubectl create namespace adp-gateway-agents --dry-run=client -o yaml | kubectl apply -f -
    INPUT_QUEUE_URL=$(cd infra && terraform output -raw gateway_input_queue_url 2>/dev/null || echo "PENDING")
    RESPONSE_QUEUE_URL=$(cd infra && terraform output -raw gateway_response_queue_url 2>/dev/null || echo "PENDING")
    SESSIONS_TABLE=$(cd infra && terraform output -raw gateway_sessions_table 2>/dev/null || echo "PENDING")
    AGENT_IMAGE="$REGISTRY/adp-agent-gateway:latest"
    sed -e "s|REPLACE_WITH_INPUT_QUEUE_URL|${INPUT_QUEUE_URL}|g" \
        -e "s|REPLACE_WITH_RESPONSE_QUEUE_URL|${RESPONSE_QUEUE_URL}|g" \
        -e "s|REPLACE_WITH_SESSIONS_TABLE_NAME|${SESSIONS_TABLE}|g" \
        -e "s|REPLACE_WITH_AGENT_IMAGE|${AGENT_IMAGE}|g" \
        gateway/k8s/keda-scaledjob.yaml | kubectl apply -f -
  else
    run_codebuild "adp-${ENVIRONMENT}-agent-gateway" "codebuild/bs-agent-gateway.yml"
  fi
  ok "Agent gateway deployed"

  warn "Store GitHub App creds in Secrets Manager (see modules/agent-factory/SETUP-GUIDE.md)"
else
  step "Step 6/7: Skipping agent-factory"
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
GW_WS=$(cd "$ROOT_DIR/modules/agent-factory/infra" && terraform output -raw gateway_ws_endpoint 2>/dev/null) || true
[ -n "$GW_WS" ] && [ "$GW_WS" != "" ] && echo "Gateway:   $GW_WS"
echo ""
echo "To destroy: $0 --destroy"
