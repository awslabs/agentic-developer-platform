#!/bin/bash
set -euo pipefail

# Deploy EKS Auto Mode cluster with ARC

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=========================================="
echo "GitHub Actions Runner - EKS Deployment"
echo "=========================================="

# Step 1: Terraform
echo ""
echo "Step 1: Deploying infrastructure with Terraform..."
cd "$ROOT_DIR/infrastructure"

if [ ! -f terraform.tfvars ]; then
    echo "ERROR: terraform.tfvars not found. Copy terraform.tfvars.example and fill in your values."
    exit 1
fi

terraform init
terraform apply -auto-approve

# Get outputs
CLUSTER_NAME=$(terraform output -raw cluster_name)
AWS_REGION=$(terraform output -raw kubeconfig_command | sed -n 's/.*--region \([^ ]*\).*/\1/p')
RUNNER_ROLE_ARN=$(terraform output -raw runner_role_arn)

echo ""
echo "Cluster: $CLUSTER_NAME"
echo "Region: $AWS_REGION"
echo "Runner Role: $RUNNER_ROLE_ARN"

# Step 2: Configure kubectl
echo ""
echo "Step 2: Configuring kubectl..."
aws eks update-kubeconfig --region "$AWS_REGION" --name "$CLUSTER_NAME"

# Wait for cluster to be ready
echo "Waiting for cluster to be ready..."
kubectl wait --for=condition=Ready nodes --all --timeout=300s 2>/dev/null || true

# Step 3: Install cert-manager
echo ""
echo "Step 3: Installing cert-manager..."
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.13.2/cert-manager.yaml

echo "Waiting for cert-manager to be ready..."
kubectl wait --for=condition=Available deployment/cert-manager -n cert-manager --timeout=120s
kubectl wait --for=condition=Available deployment/cert-manager-webhook -n cert-manager --timeout=120s

# Step 4: Create arc-systems namespace
echo ""
echo "Step 4: Creating arc-systems namespace..."
kubectl create namespace arc-systems --dry-run=client -o yaml | kubectl apply -f -

# Step 5: Install ARC controller
echo ""
echo "Step 5: Installing ARC controller..."
helm upgrade --install arc-controller \
    --namespace arc-systems \
    --values "$ROOT_DIR/helm/arc-controller-values.yaml" \
    --wait \
    oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set-controller

echo ""
echo "=========================================="
echo "Deployment complete!"
echo "=========================================="
echo ""
echo "Next steps:"
echo "1. Store your GitHub PAT in Secrets Manager:"
echo "   ./setup-secrets.sh <your-github-pat>"
echo ""
echo "2. Onboard a repository:"
echo "   ./onboard-repo.sh <repo-name>"
echo ""
echo "Runner Role ARN (for reference): $RUNNER_ROLE_ARN"
