#!/usr/bin/env bash
# =============================================================================
# DeepWiki Secrets & GitHub Integration Deployment Script
# =============================================================================
# Deploys the full secrets infrastructure and K8s manifests for DeepWiki:
#   1. Terraform: Secrets Manager + IRSA role
#   2. Docker: Build and push github-token-refresher image
#   3. K8s: Apply all DeepWiki manifests with IRSA annotation
#
# Prerequisites:
#   - AWS CLI configured with appropriate permissions
#   - kubectl configured for the target EKS cluster
#   - Terraform >= 1.0 installed
#   - Docker installed and authenticated to ECR
#
# Required Environment Variables:
#   TF_VAR_gateway_api_key        - Bedrock Gateway API key
#   TF_VAR_github_app_id          - GitHub App ID
#   TF_VAR_github_app_private_key - GitHub App private key (PEM)
#
# Optional Environment Variables:
#   AWS_REGION      - AWS region (default: us-east-1)
#   EKS_CLUSTER     - EKS cluster name (default: github-arc-runner-eks)
#   ECR_REGISTRY    - ECR registry URL (auto-detected)
#   IMAGE_TAG       - Docker image tag (default: latest)
#   SKIP_TERRAFORM  - Set to "true" to skip Terraform
#   SKIP_DOCKER     - Set to "true" to skip Docker build
#   DRY_RUN         - Set to "true" for dry-run mode
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

AWS_REGION="${AWS_REGION:-us-east-1}"
EKS_CLUSTER="${EKS_CLUSTER:-github-arc-runner-eks}"
AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo "")}"
ECR_REGISTRY="${ECR_REGISTRY:-${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
SKIP_TERRAFORM="${SKIP_TERRAFORM:-false}"
SKIP_DOCKER="${SKIP_DOCKER:-false}"
DRY_RUN="${DRY_RUN:-false}"

TERRAFORM_DIR="${PROJECT_ROOT}/infrastructure/deepwiki-secrets"
DOCKER_DIR="${PROJECT_ROOT}/docker/github-token-refresher"
K8S_DIR="${PROJECT_ROOT}/k8s/deepwiki"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }
err() { log "ERROR: $*" >&2; }

run() {
  if [[ "$DRY_RUN" == "true" ]]; then
    log "[DRY-RUN] $*"
  else
    "$@"
  fi
}

check_prereqs() {
  local missing=()
  command -v aws >/dev/null 2>&1      || missing+=("aws")
  command -v kubectl >/dev/null 2>&1   || missing+=("kubectl")
  command -v terraform >/dev/null 2>&1 || missing+=("terraform")
  command -v docker >/dev/null 2>&1    || missing+=("docker")

  if [[ ${#missing[@]} -gt 0 ]]; then
    err "Missing required tools: ${missing[*]}"
    exit 1
  fi

  if [[ -z "$AWS_ACCOUNT_ID" ]]; then
    err "Could not determine AWS Account ID. Is AWS CLI configured?"
    exit 1
  fi

  log "AWS Account: ${AWS_ACCOUNT_ID}"
  log "Region:      ${AWS_REGION}"
  log "Cluster:     ${EKS_CLUSTER}"
  log "ECR:         ${ECR_REGISTRY}"
}

# ---------------------------------------------------------------------------
# Step 1: Terraform — Secrets Manager + IRSA
# ---------------------------------------------------------------------------
deploy_terraform() {
  if [[ "$SKIP_TERRAFORM" == "true" ]]; then
    log "Skipping Terraform (SKIP_TERRAFORM=true)"
    return
  fi

  log "=== Step 1: Deploying Terraform (Secrets Manager + IRSA) ==="

  # Validate required TF vars
  for var in TF_VAR_gateway_api_key TF_VAR_github_app_id TF_VAR_github_app_private_key; do
    if [[ -z "${!var:-}" ]]; then
      err "Required environment variable ${var} is not set"
      exit 1
    fi
  done

  cd "$TERRAFORM_DIR"

  run terraform init -input=false
  run terraform plan -input=false -out=tfplan \
    -var="cluster_name=${EKS_CLUSTER}" \
    -var="aws_region=${AWS_REGION}"

  if [[ "$DRY_RUN" != "true" ]]; then
    terraform apply -input=false tfplan
    rm -f tfplan

    IRSA_ROLE_ARN=$(terraform output -raw irsa_role_arn)
    log "IRSA Role ARN: ${IRSA_ROLE_ARN}"
  fi

  cd "$PROJECT_ROOT"
}

# ---------------------------------------------------------------------------
# Step 2: Docker — Build and Push Token Refresher Image
# ---------------------------------------------------------------------------
deploy_docker() {
  if [[ "$SKIP_DOCKER" == "true" ]]; then
    log "Skipping Docker build (SKIP_DOCKER=true)"
    return
  fi

  log "=== Step 2: Building and Pushing GitHub Token Refresher Image ==="

  local image_name="${ECR_REGISTRY}/github-token-refresher:${IMAGE_TAG}"

  # Ensure ECR repository exists
  run aws ecr describe-repositories --repository-names github-token-refresher --region "$AWS_REGION" 2>/dev/null || \
    run aws ecr create-repository --repository-name github-token-refresher --region "$AWS_REGION" \
      --image-scanning-configuration scanOnPush=true

  # Login to ECR
  run aws ecr get-login-password --region "$AWS_REGION" | \
    docker login --username AWS --password-stdin "$ECR_REGISTRY"

  # Build and push
  run docker build -t "github-token-refresher:${IMAGE_TAG}" "$DOCKER_DIR"
  run docker tag "github-token-refresher:${IMAGE_TAG}" "$image_name"
  run docker push "$image_name"

  log "Image pushed: ${image_name}"
}

# ---------------------------------------------------------------------------
# Step 3: Kubernetes — Apply Manifests
# ---------------------------------------------------------------------------
deploy_k8s() {
  log "=== Step 3: Deploying Kubernetes Manifests ==="

  # Update kubeconfig
  run aws eks update-kubeconfig --region "$AWS_REGION" --name "$EKS_CLUSTER"

  # Get IRSA role ARN from Terraform output
  local irsa_role_arn="${IRSA_ROLE_ARN:-}"
  if [[ -z "$irsa_role_arn" && "$SKIP_TERRAFORM" != "true" ]]; then
    cd "$TERRAFORM_DIR"
    irsa_role_arn=$(terraform output -raw irsa_role_arn 2>/dev/null || echo "")
    cd "$PROJECT_ROOT"
  fi

  # Apply manifests in order
  log "Applying namespace..."
  run kubectl apply -f "$K8S_DIR/namespace.yaml"

  log "Applying service account..."
  run kubectl apply -f "$K8S_DIR/serviceaccount.yaml"

  # Annotate service account with IRSA role
  if [[ -n "$irsa_role_arn" ]]; then
    log "Annotating service account with IRSA role: ${irsa_role_arn}"
    run kubectl annotate serviceaccount deepwiki-sa -n deepwiki \
      "eks.amazonaws.com/role-arn=${irsa_role_arn}" --overwrite
  else
    log "WARNING: IRSA role ARN not available. Service account will not have AWS access."
  fi

  log "Applying RBAC..."
  run kubectl apply -f "$K8S_DIR/rbac.yaml"

  log "Applying secrets (fallback)..."
  run kubectl apply -f "$K8S_DIR/secret.yaml"

  log "Applying PVC..."
  run kubectl apply -f "$K8S_DIR/pvc.yaml"

  log "Applying ConfigMap..."
  run kubectl apply -f "$K8S_DIR/configmap.yaml"

  log "Applying Service..."
  run kubectl apply -f "$K8S_DIR/service.yaml"

  log "Applying NetworkPolicy..."
  run kubectl apply -f "$K8S_DIR/networkpolicy.yaml"

  # Update deployment with correct image references
  local token_image="${ECR_REGISTRY}/github-token-refresher:${IMAGE_TAG}"
  log "Applying Deployment (token refresher image: ${token_image})..."

  # Use envsubst to substitute the image references, or sed for simple replacement
  sed "s|github-token-refresher:latest|${token_image}|g" \
    "$K8S_DIR/deployment.yaml" | run kubectl apply -f -

  log "Applying Ingress..."
  run kubectl apply -f "$K8S_DIR/ingress.yaml"

  # Wait for deployment
  log "Waiting for deployment rollout..."
  run kubectl rollout status deployment/deepwiki -n deepwiki --timeout=300s

  # Show status
  log "=== Deployment Status ==="
  kubectl get pods -n deepwiki -l app=deepwiki
  kubectl get svc -n deepwiki
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
  log "=== DeepWiki Secrets & GitHub Integration Deployment ==="
  log "DRY_RUN: ${DRY_RUN}"

  check_prereqs
  deploy_terraform
  deploy_docker
  deploy_k8s

  log ""
  log "=== Deployment Complete ==="
  log ""
  log "Verify secrets init:"
  log "  kubectl logs -n deepwiki -l app=deepwiki -c secrets-init"
  log ""
  log "Verify sidecar:"
  log "  kubectl logs -n deepwiki -l app=deepwiki -c github-token-refresher"
  log ""
  log "Verify main app:"
  log "  kubectl logs -n deepwiki -l app=deepwiki -c deepwiki"
  log ""
  log "Test private repo access:"
  log "  kubectl port-forward -n deepwiki svc/deepwiki-svc 8001:8001"
  log "  curl http://localhost:8001/"
}

main "$@"
