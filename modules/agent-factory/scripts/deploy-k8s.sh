#!/bin/bash
set -e

# Configuration
CLUSTER_NAME="bedrockgw-dev-eks-cluster"
REGION="us-east-1"
NAMESPACE="agent-mail"
MANIFESTS_DIR="k8s/agent-mail"

# Default ECR registry (can be overridden)
ECR_REGISTRY="${ECR_REGISTRY:-123456789012.dkr.ecr.us-east-1.amazonaws.com}"
IMAGE_TAG="${IMAGE_TAG:-latest}"

echo "Deploying MCP Agent Mail to Kubernetes..."
echo "Cluster: $CLUSTER_NAME"
echo "Region: $REGION"
echo "Namespace: $NAMESPACE"
echo "Image: $ECR_REGISTRY/mcp-agent-mail:$IMAGE_TAG"

# Update kubeconfig for the target cluster
echo "Updating kubeconfig for cluster $CLUSTER_NAME..."
aws eks update-kubeconfig --region $REGION --name $CLUSTER_NAME

# Verify cluster access
echo "Verifying cluster access..."
kubectl cluster-info

# Check if gp3 storage class exists
echo "Checking for gp3 storage class..."
if ! kubectl get storageclass gp3 >/dev/null 2>&1; then
    echo "Warning: gp3 storage class not found. You may need to create it or use a different storage class."
    echo "Available storage classes:"
    kubectl get storageclass
fi

# Function to apply manifest with variable substitution
apply_manifest() {
    local manifest_file="$1"
    echo "Applying $manifest_file..."

    # Substitute variables in the manifest
    envsubst < "$manifest_file" | kubectl apply -f -
}

# Apply manifests in order
echo "Applying Kubernetes manifests..."

# 1. Namespace
apply_manifest "$MANIFESTS_DIR/namespace.yaml"

# 2. ServiceAccount
apply_manifest "$MANIFESTS_DIR/serviceaccount.yaml"

# 3. RBAC
apply_manifest "$MANIFESTS_DIR/rbac.yaml"

# 4. PVC
apply_manifest "$MANIFESTS_DIR/pvc.yaml"

# 5. Create bearer token secret (generates random token if not exists)
echo "Creating bearer token secret..."
kubectl create secret generic agent-mail-auth -n $NAMESPACE \
    --from-literal=bearer-token="$(openssl rand -base64 32)" \
    --dry-run=client -o yaml | kubectl apply -f -

# 6. ConfigMap
apply_manifest "$MANIFESTS_DIR/configmap.yaml"

# 7. Service
apply_manifest "$MANIFESTS_DIR/service.yaml"

# 8. Deployment (with image substitution)
apply_manifest "$MANIFESTS_DIR/deployment.yaml"

# Wait for deployment to be ready
echo "Waiting for deployment to be ready..."
kubectl rollout status deployment/agent-mail -n $NAMESPACE --timeout=300s

# Show deployment status
echo "Deployment status:"
kubectl get pods -n $NAMESPACE -l app=mcp-agent-mail

echo "Services:"
kubectl get svc -n $NAMESPACE

echo "PVC status:"
kubectl get pvc -n $NAMESPACE

# Show recent events
echo "Recent events:"
kubectl get events -n $NAMESPACE --sort-by='.lastTimestamp' | tail -10

echo "Deployment complete!"
echo ""
echo "To check logs:"
echo "  kubectl logs -f deployment/agent-mail -n $NAMESPACE"
echo ""
echo "To check health:"
echo "  kubectl port-forward -n $NAMESPACE svc/agent-mail-svc 8765:8765"
echo "  curl http://localhost:8765/health/liveness"