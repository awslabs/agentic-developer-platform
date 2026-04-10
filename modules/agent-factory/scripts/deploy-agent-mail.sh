#!/bin/bash
# =============================================================================
# MCP Agent Mail - Complete Deployment Script
# =============================================================================
# Deploys the MCP Agent Mail application to bedrockgw-dev-eks-cluster.
#
# Prerequisites:
#   - AWS CLI configured with EKS and ECR permissions
#   - kubectl installed
#   - Docker installed (for building)
#   - envsubst available
#
# Usage:
#   export ECR_REGISTRY="605440105851.dkr.ecr.us-east-1.amazonaws.com"
#   ./scripts/deploy-agent-mail.sh [--build] [--skip-ingress]
#
# Options:
#   --build          Build and push Docker image before deploying
#   --skip-ingress   Skip ingress creation (if DNS/ACM not ready)
#   --dry-run        Show what would be applied without actually applying
# =============================================================================

set -euo pipefail

# --- Configuration ---
CLUSTER_NAME="bedrockgw-dev-eks-cluster"
REGION="us-east-1"
NAMESPACE="agent-mail"
MANIFESTS_DIR="k8s/agent-mail"
DOCKERFILE_PATH="docker/agent-mail/Dockerfile"
IMAGE_NAME="mcp-agent-mail"

ECR_REGISTRY="${ECR_REGISTRY:-605440105851.dkr.ecr.us-east-1.amazonaws.com}"
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || echo 'latest')}"

# --- Parse arguments ---
BUILD_IMAGE=false
SKIP_INGRESS=false
DRY_RUN=false

for arg in "$@"; do
    case $arg in
        --build) BUILD_IMAGE=true ;;
        --skip-ingress) SKIP_INGRESS=true ;;
        --dry-run) DRY_RUN=true ;;
        *) echo "Unknown option: $arg"; exit 1 ;;
    esac
done

KUBECTL_CMD="kubectl"
if [ "$DRY_RUN" = true ]; then
    echo "[DRY-RUN MODE] No changes will be applied."
    KUBECTL_CMD="kubectl --dry-run=client"
fi

echo "============================================"
echo " MCP Agent Mail Deployment"
echo "============================================"
echo "Cluster:  $CLUSTER_NAME"
echo "Region:   $REGION"
echo "Registry: $ECR_REGISTRY"
echo "Image:    $ECR_REGISTRY/$IMAGE_NAME:$IMAGE_TAG"
echo "============================================"

# --- Step 0: Build and push image (optional) ---
if [ "$BUILD_IMAGE" = true ]; then
    echo ""
    echo "--- Step 0: Building and pushing container image ---"
    ./scripts/build-and-push.sh
fi

# --- Step 1: Configure kubectl ---
echo ""
echo "--- Step 1: Configuring kubectl ---"
aws eks update-kubeconfig --region "$REGION" --name "$CLUSTER_NAME"
echo "Verifying cluster access..."
kubectl cluster-info || { echo "ERROR: Cannot access cluster. Check IAM permissions."; exit 1; }

# --- Step 2: Check storage class ---
echo ""
echo "--- Step 2: Checking storage class ---"
if kubectl get storageclass ebs-gp3 >/dev/null 2>&1; then
    echo "ebs-gp3 storage class found (EKS-managed CSI driver, gp3 volumes)."
elif kubectl get storageclass auto-ebs-sc >/dev/null 2>&1; then
    echo "auto-ebs-sc storage class found (EKS Auto Mode)."
    echo "NOTE: PVC uses ebs-gp3. Update k8s/agent-mail/pvc.yaml if auto-ebs-sc is preferred."
else
    echo "WARNING: ebs-gp3 storage class not found. Creating it..."
    cat <<SCEOF | kubectl apply -f -
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: ebs-gp3
provisioner: ebs.csi.eks.amazonaws.com
parameters:
  type: gp3
  fsType: ext4
reclaimPolicy: Delete
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
SCEOF
fi

# --- Step 3: Apply core manifests in dependency order ---
echo ""
echo "--- Step 3: Applying core K8s manifests ---"

apply_manifest() {
    local file="$1"
    local desc="$2"
    echo "  Applying: $desc ($file)"
    if [ "$DRY_RUN" = true ]; then
        envsubst < "$file" | kubectl apply --dry-run=client -f - 2>&1 | sed 's/^/    /'
    else
        envsubst < "$file" | kubectl apply -f - 2>&1 | sed 's/^/    /'
    fi
}

# 3a. Namespace
apply_manifest "$MANIFESTS_DIR/namespace.yaml" "Namespace"

# 3b. ServiceAccount
apply_manifest "$MANIFESTS_DIR/serviceaccount.yaml" "ServiceAccount"

# 3c. RBAC
apply_manifest "$MANIFESTS_DIR/rbac.yaml" "RBAC (Role + RoleBinding)"

# 3d. PVC
apply_manifest "$MANIFESTS_DIR/pvc.yaml" "PersistentVolumeClaim"

# --- Step 4: Create bearer token secret ---
echo ""
echo "--- Step 4: Creating bearer token secret ---"
if [ "$DRY_RUN" = true ]; then
    echo "  [DRY-RUN] Would create secret agent-mail-auth with random bearer token"
else
    # Generate a cryptographically strong bearer token
    BEARER_TOKEN=$(openssl rand -base64 32)
    kubectl create secret generic agent-mail-auth -n "$NAMESPACE" \
        --from-literal=bearer-token="$BEARER_TOKEN" \
        --dry-run=client -o yaml | kubectl apply -f -
    echo "  Secret agent-mail-auth created/updated."
    echo "  To retrieve the bearer token:"
    echo "    kubectl get secret agent-mail-auth -n $NAMESPACE -o jsonpath='{.data.bearer-token}' | base64 -d"
fi

# --- Step 5: Apply ConfigMap ---
echo ""
echo "--- Step 5: Applying ConfigMap ---"
apply_manifest "$MANIFESTS_DIR/configmap.yaml" "ConfigMap"

# --- Step 6: Apply Service ---
echo ""
echo "--- Step 6: Applying Service ---"
apply_manifest "$MANIFESTS_DIR/service.yaml" "Service"

# --- Step 7: Apply Deployment with image substitution ---
echo ""
echo "--- Step 7: Applying Deployment ---"
echo "  Image: $ECR_REGISTRY/$IMAGE_NAME:$IMAGE_TAG"
export ECR_REGISTRY IMAGE_TAG
apply_manifest "$MANIFESTS_DIR/deployment.yaml" "Deployment"

# --- Step 8: Wait for rollout ---
echo ""
echo "--- Step 8: Waiting for deployment rollout ---"
if [ "$DRY_RUN" = false ]; then
    kubectl rollout status deployment/agent-mail -n "$NAMESPACE" --timeout=180s || {
        echo "WARNING: Deployment rollout timed out. Checking pod status..."
        kubectl describe pods -n "$NAMESPACE" -l app=mcp-agent-mail | tail -30
        exit 1
    }
fi

# --- Step 9: Apply Ingress (optional) ---
if [ "$SKIP_INGRESS" = false ]; then
    echo ""
    echo "--- Step 9: Applying Ingress ---"
    echo "  WARNING: Ensure you have updated ingress.yaml with:"
    echo "    - Actual ACM certificate ARN"
    echo "    - Correct CIDR restrictions"
    echo "    - Correct hostname (if not api.bedrockgw.dev)"
    apply_manifest "$MANIFESTS_DIR/ingress.yaml" "Ingress"
else
    echo ""
    echo "--- Step 9: Skipping Ingress (--skip-ingress flag) ---"
fi

# --- Step 10: Verify deployment ---
echo ""
echo "============================================"
echo " Deployment Verification"
echo "============================================"

if [ "$DRY_RUN" = false ]; then
    echo ""
    echo "Pods:"
    kubectl get pods -n "$NAMESPACE" -l app=mcp-agent-mail -o wide

    echo ""
    echo "Services:"
    kubectl get svc -n "$NAMESPACE"

    echo ""
    echo "PVC:"
    kubectl get pvc -n "$NAMESPACE"

    echo ""
    echo "Secrets:"
    kubectl get secrets -n "$NAMESPACE"

    if [ "$SKIP_INGRESS" = false ]; then
        echo ""
        echo "Ingress:"
        kubectl get ingress -n "$NAMESPACE"
    fi

    echo ""
    echo "Recent Events:"
    kubectl get events -n "$NAMESPACE" --sort-by='.lastTimestamp' 2>/dev/null | tail -10

    # --- Step 11: Health check via port-forward ---
    echo ""
    echo "--- Health Check ---"
    echo "To test the health endpoint, run:"
    echo "  kubectl port-forward -n $NAMESPACE svc/agent-mail-svc 8765:8765 &"
    echo "  curl http://localhost:8765/health/liveness"
    echo ""
    echo "To get the bearer token for authenticated requests:"
    echo "  kubectl get secret agent-mail-auth -n $NAMESPACE -o jsonpath='{.data.bearer-token}' | base64 -d"
fi

echo ""
echo "============================================"
echo " Deployment Complete!"
echo "============================================"
